import sys
import os

# Add the project root to sys.path to support standalone execution
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import gc
import time
import ctypes
import logging
import threading
from typing import Optional
from src.metrics import get_meter

logger = logging.getLogger("cpu_worker")

# Stored as plain str so static analysers cannot narrow it to a platform literal,
# keeping the cross-platform branches reachable in their eyes.
_PLATFORM: str = sys.platform


class Timespec(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_long),
        ("tv_nsec", ctypes.c_long)
    ]


class SchedParam(ctypes.Structure):
    _fields_ = [("sched_priority", ctypes.c_int)]


SCHED_FIFO = 1


class CPUWorker:
    def __init__(self, interval_ms: float = 1.0, rt_threshold_ns: int = 100_000,
                 collect_samples: bool = False):
        self.interval_ns = int(interval_ms * 1_000_000)
        self.libc = self._load_libc()
        self.running = False

        self.min_jitter = float('inf')
        self.max_jitter = float('-inf')
        self.total_jitter = 0
        self.count = 0
        self.rt_threshold_ns = rt_threshold_ns

        # Pre-allocated Timespec to avoid per-call heap allocation in the hot loop
        self._ts = Timespec()

        # Optional full sample log for percentile computation (enabled in headless mode)
        self._samples: list[int] = [] if collect_samples else []
        self._collect_samples = collect_samples

        self._setup_otel()

    def _setup_otel(self):
        try:
            meter = get_meter()
            self.otel_max_jitter = meter.create_gauge("cpu.jitter.max", unit="ns", description="Max wake-up jitter")
            self.otel_min_jitter = meter.create_gauge("cpu.jitter.min", unit="ns", description="Min wake-up jitter")
            self.otel_avg_jitter = meter.create_gauge("cpu.jitter.avg", unit="ns", description="Average wake-up jitter")
        except Exception as e:
            logger.warning(f"OTel setup failed: {e}")

    def _load_libc(self):
        try:
            if _PLATFORM == "darwin":
                return ctypes.CDLL("/usr/lib/libc.dylib", use_errno=True)
            return ctypes.CDLL("libc.so.6", use_errno=True)
        except Exception:
            return ctypes.CDLL(None, use_errno=True)

    def _nanosleep(self, ns: int):
        if ns <= 0:
            return
        self._ts.tv_sec = ns // 1_000_000_000
        self._ts.tv_nsec = ns % 1_000_000_000
        self.libc.nanosleep(ctypes.byref(self._ts), None)

    def _set_affinity(self, core: int):
        if _PLATFORM == "linux":
            try:
                os.sched_setaffinity(0, {core})
                logger.debug(f"Pinned to CPU core {core} (Linux)")
            except Exception as e:
                logger.warning(f"Could not set affinity: {e}")
        elif _PLATFORM == "darwin":
            try:
                mach_thread = self.libc.mach_thread_self()
                policy_info = (ctypes.c_int * 1)(core)
                res = self.libc.thread_policy_set(mach_thread, 4, ctypes.byref(policy_info), 1)
                if res == 0:
                    logger.debug(f"Set affinity tag {core} (macOS)")
                else:
                    logger.warning(f"thread_policy_set failed with code {res}")
            except Exception as e:
                logger.warning(f"macOS affinity failed: {e}")
        else:
            logger.debug("Affinity not implemented for this platform")

    def _set_rt_priority(self):
        """Elevate the measurement thread to SCHED_FIFO so the OS treats it as an RT task.
        Without this, jitter results reflect normal-priority scheduling, not RT capability."""
        if _PLATFORM == "linux":
            try:
                param = SchedParam(sched_priority=99)
                ret = self.libc.sched_setscheduler(0, SCHED_FIFO, ctypes.byref(param))
                if ret == 0:
                    logger.debug("Scheduler elevated to SCHED_FIFO priority 99")
                else:
                    errno = ctypes.get_errno()
                    logger.warning(
                        f"sched_setscheduler failed (errno={errno}). "
                        "Run as root for RT scheduling — results will reflect SCHED_OTHER, not true RT capability."
                    )
            except Exception as e:
                logger.warning(f"Could not set RT scheduler: {e}")
        else:
            logger.debug("RT scheduler elevation (SCHED_FIFO) not supported on this platform")

    def run(self, duration_s: Optional[float] = None, core: int = 0, stop_event: Optional[threading.Event] = None):
        self._set_affinity(core)
        self._set_rt_priority()
        self.running = True
        start_time = time.time()

        next_target = time.clock_gettime_ns(time.CLOCK_MONOTONIC) + self.interval_ns

        logger.info(f"Starting CPU jitter test with {self.interval_ns / 1_000_000}ms interval")

        # Move existing objects to the permanent generation and disable the cyclic GC
        # to eliminate unpredictable collection pauses from the measurement loop.
        gc.freeze()
        gc.disable()
        try:
            while self.running:
                if stop_event and stop_event.is_set():
                    self.running = False
                    break

                now = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
                sleep_time = next_target - now
                if sleep_time > 0:
                    self._nanosleep(sleep_time)

                actual_wake = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
                jitter = actual_wake - next_target

                if jitter < self.min_jitter:
                    self.min_jitter = jitter
                if jitter > self.max_jitter:
                    self.max_jitter = jitter
                self.total_jitter += jitter
                self.count += 1
                if self._collect_samples:
                    self._samples.append(jitter)

                if self.count % 100 == 0:
                    avg = self.total_jitter / self.count
                    self._update_test_state(avg)
                    if self.count % 1000 == 0:
                        logger.debug(f"Jitter (ns): min={self.min_jitter}, max={self.max_jitter}, avg={avg:.2f}")
                        self._update_otel(avg)

                next_target += self.interval_ns

                if duration_s and (time.time() - start_time) >= duration_s:
                    break
        except KeyboardInterrupt:
            self.running = False
        finally:
            gc.enable()
            gc.collect()

        logger.info("CPU jitter test stopped")
        self.report()

    def _update_test_state(self, avg):
        if not hasattr(self, 'shared_res'):
            return
        data = {
            'avg': avg,
            'max': self.max_jitter,
            'min': self.min_jitter,
            'rt_ready': self.is_rt_ready(),
        }
        lock = getattr(self, 'shared_res_lock', None)
        if lock:
            with lock:
                self.shared_res.update(data)
        else:
            self.shared_res.update(data)

    def _update_otel(self, avg):
        try:
            self.otel_max_jitter.set(self.max_jitter)
            self.otel_min_jitter.set(self.min_jitter)
            self.otel_avg_jitter.set(avg)
        except Exception:
            pass

    def percentiles(self) -> dict:
        """Return p50/p95/p99/p99.9 from collected samples (requires collect_samples=True)."""
        if not self._samples:
            return {}
        s = sorted(self._samples)
        n = len(s)
        def pct(p: float) -> int:
            return s[min(int(n * p / 100), n - 1)]
        return {"p50": pct(50), "p95": pct(95), "p99": pct(99), "p99_9": pct(99.9)}

    def is_rt_ready(self) -> bool:
        if self.count == 0:
            return False
        return self.max_jitter < self.rt_threshold_ns

    def report(self):
        if self.count > 0:
            avg = self.total_jitter / self.count
            rt_ready = self.is_rt_ready()
            print(f"\n--- Final Results ---")
            print(f"Samples: {self.count}")
            print(f"Min Jitter: {self.min_jitter} ns")
            print(f"Max Jitter: {self.max_jitter} ns")
            print(f"Avg Jitter: {avg:.2f} ns")
            print(f"RT Ready: {'YES' if rt_ready else 'NO'} (Threshold: {self.rt_threshold_ns} ns)")


if __name__ == "__main__":
    import argparse, json
    from datetime import datetime, timezone
    from src.version import VERSION

    parser = argparse.ArgumentParser(description="RT CPU jitter measurement — headless mode")
    parser.add_argument("--interval",  type=float, default=1.0,       help="Sleep interval in ms")
    parser.add_argument("--duration",  type=float, default=60.0,      help="Test duration in seconds")
    parser.add_argument("--core",      type=int,   default=1,         help="CPU core to pin to")
    parser.add_argument("--threshold", type=int,   default=100_000,   help="RT threshold in ns")
    parser.add_argument("--label",     type=str,   default="native",  help="Tag added to the JSON output (e.g. 'docker')")
    parser.add_argument("--output",    type=str,   default=None,      help="Write JSON results to this file (- for stdout)")
    parser.add_argument("--debug",     action="store_true",           help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')

    worker = CPUWorker(interval_ms=args.interval, rt_threshold_ns=args.threshold,
                       collect_samples=args.output is not None)
    worker.run(duration_s=args.duration, core=args.core)

    if args.output:
        avg = worker.total_jitter / worker.count if worker.count else 0
        result = {
            "label":            args.label,
            "version":          VERSION,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "platform":         _PLATFORM,
            "duration_s":       args.duration,
            "interval_ms":      args.interval,
            "core":             args.core,
            "rt_threshold_ns":  args.threshold,
            "samples":          worker.count,
            "jitter_ns": {
                "min": worker.min_jitter,
                "max": worker.max_jitter,
                "avg": round(avg, 2),
                **worker.percentiles(),
            },
            "rt_ready": worker.is_rt_ready(),
        }
        text = json.dumps(result, indent=2)
        if args.output == "-":
            print(text)
        else:
            with open(args.output, "w") as fh:
                fh.write(text + "\n")
            print(f"Results → {args.output}")
