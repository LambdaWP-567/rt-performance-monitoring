import time
import ctypes
import os
import sys
import logging
from typing import Optional
from src.metrics import get_meter

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("cpu_worker")

class Timespec(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_long),
        ("tv_nsec", ctypes.c_long)
    ]

class CPUWorker:
    def __init__(self, interval_ms: float = 1.0):
        self.interval_ns = int(interval_ms * 1_000_000)
        self.libc = self._load_libc()
        self.running = False

        self.min_jitter = float('inf')
        self.max_jitter = float('-inf')
        self.total_jitter = 0
        self.count = 0
        self.rt_threshold_ns = 100_000 # 100 microseconds

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
            if sys.platform == "darwin":
                return ctypes.CDLL("/usr/lib/libc.dylib")
            return ctypes.CDLL("libc.so.6")
        except Exception:
            return ctypes.CDLL(None)

    def _nanosleep(self, ns: int):
        if ns <= 0:
            return
        ts = Timespec()
        ts.tv_sec = ns // 1_000_000_000
        ts.tv_nsec = ns % 1_000_000_000
        self.libc.nanosleep(ctypes.byref(ts), None)

    def _set_affinity(self, core: int):
        if sys.platform == "linux":
            try:
                os.sched_setaffinity(0, {core})
                logger.info(f"Pinned to CPU core {core} (Linux)")
            except Exception as e:
                logger.warning(f"Could not set affinity: {e}")
        elif sys.platform == "darwin":
            # THREAD_AFFINITY_POLICY = 4
            # kern_return_t thread_policy_set(thread_t thread, thread_policy_flavor_t flavor,
            #                                 thread_policy_t policy_info, mach_msg_type_number_t count)
            try:
                # Get current mach thread
                mach_thread = self.libc.mach_thread_self()
                policy_info = (ctypes.c_int * 1)(core)
                res = self.libc.thread_policy_set(mach_thread, 4, ctypes.byref(policy_info), 1)
                if res == 0:
                    logger.info(f"Set affinity tag {core} (macOS)")
                else:
                    logger.warning(f"thread_policy_set failed with code {res}")
            except Exception as e:
                logger.warning(f"macOS affinity failed: {e}")
        else:
            logger.info("Affinity not implemented for this platform")

    def run(self, duration_s: Optional[float] = None, core: int = 0, stop_event: Optional[threading.Event] = None):
        self._set_affinity(core)
        self.running = True
        start_time = time.time()

        # Initial target
        next_target = time.clock_gettime_ns(time.CLOCK_MONOTONIC) + self.interval_ns

        logger.info(f"Starting CPU jitter test with {self.interval_ns/1_000_000}ms interval")

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

                # Update stats
                self.min_jitter = min(self.min_jitter, jitter)
                self.max_jitter = max(self.max_jitter, jitter)
                self.total_jitter += jitter
                self.count += 1

                if self.count % 1000 == 0:
                    avg = self.total_jitter / self.count
                    logger.info(f"Jitter (ns): min={self.min_jitter}, max={self.max_jitter}, avg={avg:.2f}")
                    self._update_otel(avg)

                next_target += self.interval_ns

                if duration_s and (time.time() - start_time) >= duration_s:
                    break
        except KeyboardInterrupt:
            self.running = False

        logger.info("CPU jitter test stopped")
        self.report()

    def _update_otel(self, avg):
        try:
            self.otel_max_jitter.set(self.max_jitter)
            self.otel_min_jitter.set(self.min_jitter)
            self.otel_avg_jitter.set(avg)
        except Exception:
            pass

    def is_rt_ready(self) -> bool:
        """
        Judge if the system is RT ready based on max jitter.
        Threshold: 100 microseconds.
        """
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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=1.0, help="Interval in ms")
    parser.add_argument("--duration", type=float, default=5.0, help="Duration in seconds")
    parser.add_argument("--core", type=int, default=0, help="CPU core to bind to")
    args = parser.parse_args()

    worker = CPUWorker(interval_ms=args.interval)
    worker.run(duration_s=args.duration, core=args.core)
