from fastapi import FastAPI, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os
import threading
import time
import logging
from contextlib import asynccontextmanager
from typing import Dict, Optional
from pydantic import BaseModel
from src.cpu_worker import CPUWorker
from src.network_worker import NetworkWorker
from src.metrics import setup_metrics
from src.version import VERSION

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("rt_app")

# Thread-safe global state
class TestState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.start_time = None
        self.duration = 0
        self.cpu_results = {}
        self.net_results = {}
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
            elapsed = 0
            if self.running and self.start_time:
                elapsed = time.time() - self.start_time
            return {
                "running": self.running,
                "elapsed_seconds": elapsed,
                "remaining_seconds": max(0, self.duration - elapsed) if self.duration else 0,
                "rt_ready": self.rt_ready,
                "results": {
                    "cpu": self.cpu_results,
                    "network": self.net_results
                }
            }

test_state = TestState()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"RT Performance Monitor Version {VERSION} Starting...")
    setup_metrics()
    yield
    # Cleanup if needed

app = FastAPI(title="RT Performance Monitor", lifespan=lifespan)

# Static UI
@app.get("/")
async def read_index():
    return FileResponse('src/static/index.html')

class TestConfig(BaseModel):
    duration_min: float = 15.0
    interval_cpu_ms: float = 1.0
    interval_net_s: float = 1.0
    core: int = 0
    interface: str = "lo"
    cyclic: bool = False

def run_test_cycle(config: TestConfig):
    duration_s = config.duration_min * 60
    test_state.stop_event.clear()

    enable_net = os.getenv("ENABLE_NET_TEST", "false").lower() == "true"
    default_interface = os.getenv("NET_INTERFACE", config.interface)

    while not test_state.stop_event.is_set():
        cpu_results = {"min": 0, "max": 0, "avg": 0}
        net_results = {"min": 0, "max": 0, "avg": 0}
        test_state.update(running=True, start_time=time.time(), duration=duration_s, cpu_results=cpu_results, net_results=net_results)

        logger.info(f"Starting test cycle: duration={config.duration_min}min, cyclic={config.cyclic}")

        cpu_worker = CPUWorker(interval_ms=config.interval_cpu_ms)
        cpu_worker.shared_res = cpu_results
        t1 = threading.Thread(target=cpu_worker.run, kwargs={'duration_s': duration_s, 'core': config.core, 'stop_event': test_state.stop_event})
        t1.start()

        t2 = None
        if enable_net:
            net_worker = NetworkWorker(interface=default_interface, interval_s=config.interval_net_s)
            net_worker.shared_res = net_results
            t2 = threading.Thread(target=net_worker.run, kwargs={'duration_s': duration_s, 'stop_event': test_state.stop_event})
            t2.start()
        else:
            logger.info("Network test disabled via environment variable.")

        t1.join()
        if t2:
            t2.join()

        cpu_res = {
            "min": cpu_worker.min_jitter,
            "max": cpu_worker.max_jitter,
            "avg": cpu_worker.total_jitter / cpu_worker.count if cpu_worker.count > 0 else 0
        }
        net_res = {"min": 0, "max": 0, "avg": 0}
        if enable_net:
            net_res = {
                "min": net_worker.min_rtt,
                "max": net_worker.max_rtt,
                "avg": net_worker.total_rtt / net_worker.count if net_worker.count > 0 else 0
            }

        test_state.update(rt_ready=cpu_worker.is_rt_ready())

        logger.info(f"Test cycle completed. Results: CPU Avg Jitter={cpu_res['avg']:.2f}ns, Net Avg RTT={'N/A' if not enable_net else f'{net_res['avg']:.4f}ms'}")

        if not config.cyclic or not test_state.running:
            break

        logger.info("Restarting cyclic test...")
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

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("APP_PORT", 8000))
    try:
        uvicorn.run(app, host="0.0.0.0", port=port)
    except OSError as e:
        if e.errno == 98:
            print(f"\n[ERROR] Port {port} is already in use.")
            print("Please ensure no other instances of the RT monitor are running.")
            print(f"To kill existing process: kill $(lsof -t -i:{port})")
            print("Or change port using APP_PORT environment variable.\n")
        else:
            raise e
