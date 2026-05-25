import sys
import os

# Add the project root to sys.path to support standalone execution
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
import threading
import time
import logging
from contextlib import asynccontextmanager
from pydantic import BaseModel
from src.cpu_worker import CPUWorker
from src.network_worker import NetworkWorker
from src.metrics import setup_metrics
from src.version import VERSION

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("rt_app")

STATIC_DIR = Path(__file__).parent / "static"

# Runtime-configurable RT thresholds
_threshold_lock = threading.Lock()
_cpu_threshold_ns: int = 100_000  # 100 µs default


def get_cpu_threshold_ns() -> int:
    with _threshold_lock:
        return _cpu_threshold_ns


class TestState:
    def __init__(self):
        self.lock = threading.Lock()
        self.results_lock = threading.Lock()  # guards cpu_results and net_results dict contents
        self.running = False
        self.start_time = None
        self.duration = 0
        self.cpu_results: dict = {}
        self.net_results: dict = {}
        self.rt_ready = False
        self.cyclic = False
        self.thread = None
        self.stop_event = threading.Event()

    def update(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def get_dict(self):
        with self.lock:
            running = self.running
            start_time = self.start_time
            duration = self.duration
            rt_ready_fallback = self.rt_ready

        elapsed = 0
        if running and start_time:
            elapsed = time.time() - start_time

        with self.results_lock:
            cpu_res = dict(self.cpu_results)
            net_res = dict(self.net_results)

        # Use live rt_ready from the worker when available; fall back to last completed result
        live_rt_ready = cpu_res.pop('rt_ready', rt_ready_fallback)

        return {
            "running": running,
            "elapsed_seconds": elapsed,
            "remaining_seconds": max(0, duration - elapsed) if duration else 0,
            "rt_ready": live_rt_ready,
            "results": {
                "cpu": cpu_res,
                "network": net_res
            }
        }


test_state = TestState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info(f"RT Performance Monitor Version {VERSION} Starting...")

    enable_net = os.getenv("ENABLE_NET_TEST", "false").lower() == "true"
    net_iface = os.getenv("NET_INTERFACE", "lo")
    logger.debug(f"Network Performance Test: {'ENABLED on ' + net_iface if enable_net else 'DISABLED (Opt-in via ENABLE_NET_TEST=true)'}")

    setup_metrics()
    yield


app = FastAPI(title="RT Performance Monitor", lifespan=lifespan)


@app.get("/")
async def read_index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


class TestConfig(BaseModel):
    duration_min: float = 15.0
    interval_cpu_ms: float = 1.0
    interval_net_s: float = 1.0
    core: int = 0
    interface: str = "lo"
    cyclic: bool = False


class ThresholdConfig(BaseModel):
    cpu_jitter_threshold_us: float


@app.get("/config")
async def get_config():
    ns = get_cpu_threshold_ns()
    return {"cpu_jitter_threshold_us": ns / 1000, "cpu_jitter_threshold_ns": ns}


@app.post("/config")
async def set_config(cfg: ThresholdConfig):
    global _cpu_threshold_ns
    if not (10 <= cfg.cpu_jitter_threshold_us <= 10_000):
        return {"status": "error", "message": "Threshold must be between 10 and 10 000 µs"}
    with _threshold_lock:
        _cpu_threshold_ns = int(cfg.cpu_jitter_threshold_us * 1000)
    logger.info(f"RT threshold updated to {cfg.cpu_jitter_threshold_us} µs ({_cpu_threshold_ns} ns)")
    return {"status": "updated", "cpu_jitter_threshold_us": cfg.cpu_jitter_threshold_us}


def run_test_cycle(config: TestConfig):
    duration_s = config.duration_min * 60
    test_state.stop_event.clear()

    enable_net = os.getenv("ENABLE_NET_TEST", "false").lower() == "true"
    default_interface = os.getenv("NET_INTERFACE", config.interface)

    while not test_state.stop_event.is_set():
        cpu_results: dict = {"min": 0, "max": 0, "avg": 0}
        net_results: dict = {"min": 0, "max": 0, "avg": 0}

        with test_state.results_lock:
            test_state.cpu_results = cpu_results
            test_state.net_results = net_results
        test_state.update(running=True, start_time=time.time(), duration=duration_s)

        logger.info(f"Starting test cycle: duration={config.duration_min}min, cyclic={config.cyclic}")

        cpu_worker = CPUWorker(interval_ms=config.interval_cpu_ms, rt_threshold_ns=get_cpu_threshold_ns())
        cpu_worker.shared_res = cpu_results
        cpu_worker.shared_res_lock = test_state.results_lock
        t1 = threading.Thread(
            target=cpu_worker.run,
            kwargs={'duration_s': duration_s, 'core': config.core, 'stop_event': test_state.stop_event}
        )
        t1.start()

        t2 = None
        if enable_net:
            net_worker = NetworkWorker(interface=default_interface, interval_s=config.interval_net_s)
            net_worker.shared_res = net_results
            net_worker.shared_res_lock = test_state.results_lock
            t2 = threading.Thread(
                target=net_worker.run,
                kwargs={'duration_s': duration_s, 'stop_event': test_state.stop_event}
            )
            t2.start()
        else:
            logger.debug("Network test disabled via environment variable.")

        t1.join()
        if t2:
            t2.join()

        # Store final authoritative results (workers may have last updated at a 100-sample boundary)
        final_cpu = {
            "min": cpu_worker.min_jitter,
            "max": cpu_worker.max_jitter,
            "avg": cpu_worker.total_jitter / cpu_worker.count if cpu_worker.count > 0 else 0,
        }
        final_net: dict = {"min": 0, "max": 0, "avg": 0}
        if enable_net:
            final_net = {
                "min": net_worker.min_rtt,
                "max": net_worker.max_rtt,
                "avg": net_worker.total_rtt / net_worker.count if net_worker.count > 0 else 0,
            }

        with test_state.results_lock:
            test_state.cpu_results.update(final_cpu)
            test_state.net_results.update(final_net)
        test_state.update(rt_ready=cpu_worker.is_rt_ready())

        net_avg_str = 'N/A' if not enable_net else f"{final_net['avg']:.4f}ms"
        logger.info(
            f"Test cycle completed. CPU Avg Jitter={final_cpu['avg']:.2f}ns, Net Avg RTT={net_avg_str}"
        )

        if not config.cyclic or not test_state.running:
            break

        logger.debug("Restarting cyclic test...")
        time.sleep(1)

    test_state.update(running=False)


@app.post("/start")
async def start_test(config: TestConfig):
    if test_state.running:
        return {"status": "error", "message": "Test already running"}

    test_state.update(cyclic=config.cyclic)
    test_state.stop_event.clear()
    thread = threading.Thread(target=run_test_cycle, args=(config,))
    test_state.update(thread=thread)
    thread.start()

    return {"status": "started", "config": config}


@app.get("/status")
async def get_status():
    status = test_state.get_dict()
    status["version"] = VERSION
    return status


@app.post("/stop")
async def stop_test():
    test_state.stop_event.set()
    test_state.update(running=False)
    return {"status": "stopping"}


@app.post("/reset")
async def reset_results():
    if test_state.running:
        return {"status": "error", "message": "Cannot reset while a test is running. Stop it first."}
    with test_state.results_lock:
        test_state.cpu_results.clear()
        test_state.net_results.clear()
    test_state.update(rt_ready=False, start_time=None, duration=0)
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn
    import socket
    import argparse

    parser = argparse.ArgumentParser(description="RT Performance Monitor")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging (jitter samples, affinity, scheduler details)",
    )
    args = parser.parse_args()

    # basicConfig already ran at import time, so use setLevel to update the root logger.
    # access_log=False tells uvicorn not to emit per-request lines at all in normal mode.
    logging.getLogger().setLevel(logging.DEBUG if args.debug else logging.INFO)

    def is_port_in_use(port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('localhost', port)) == 0

    requested_port = os.getenv("APP_PORT")
    if requested_port:
        port = int(requested_port)
    else:
        port = 8000
        while is_port_in_use(port) and port < 8010:
            port += 1

    if port != 8000 and not requested_port:
        print(f"[INFO] Port 8000 was busy. Auto-selected Port {port} for Web UI.")

    try:
        uvicorn.run(app, host="0.0.0.0", port=port,
                    log_level="debug" if args.debug else "info",
                    access_log=args.debug)
    except OSError as e:
        if e.errno == 98:
            print(f"\n[ERROR] The Web UI Dashboard could not start because Port {port} is already in use.")
            print("This is NOT a Network Performance Test error, but a conflict for the Web server.")
            print("Please ensure no other instances of the RT monitor are running.")
            print(f"To kill existing process: kill $(lsof -t -i:{port})")
            print("Or change the Dashboard port using APP_PORT environment variable (e.g. APP_PORT=8080).\n")
            os._exit(1)
        else:
            raise e
