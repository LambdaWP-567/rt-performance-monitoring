from fastapi import FastAPI, BackgroundTasks
import threading
import time
import logging
from contextlib import asynccontextmanager
from typing import Dict, Optional
from pydantic import BaseModel
from src.cpu_worker import CPUWorker
from src.network_worker import NetworkWorker
from src.metrics import setup_metrics

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
    setup_metrics()
    yield
    # Cleanup if needed

app = FastAPI(title="RT Performance Monitor", lifespan=lifespan)

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

    while not test_state.stop_event.is_set():
        test_state.update(running=True, start_time=time.time(), duration=duration_s)

        logger.info(f"Starting test cycle: duration={config.duration_min}min, cyclic={config.cyclic}")

        cpu_worker = CPUWorker(interval_ms=config.interval_cpu_ms)
        net_worker = NetworkWorker(interface=config.interface, interval_s=config.interval_net_s)

        t1 = threading.Thread(target=cpu_worker.run, kwargs={'duration_s': duration_s, 'core': config.core, 'stop_event': test_state.stop_event})
        t2 = threading.Thread(target=net_worker.run, kwargs={'duration_s': duration_s, 'stop_event': test_state.stop_event})

        t1.start()
        t2.start()

        t1.join()
        t2.join()

        cpu_res = {
            "min": cpu_worker.min_jitter,
            "max": cpu_worker.max_jitter,
            "avg": cpu_worker.total_jitter / cpu_worker.count if cpu_worker.count > 0 else 0
        }
        net_res = {
            "min": net_worker.min_rtt,
            "max": net_worker.max_rtt,
            "avg": net_worker.total_rtt / net_worker.count if net_worker.count > 0 else 0
        }

        test_state.update(cpu_results=cpu_res, net_results=net_res, rt_ready=cpu_worker.is_rt_ready())

        logger.info(f"Test cycle completed. Results: CPU Avg Jitter={cpu_res['avg']:.2f}ns, Net Avg RTT={net_res['avg']:.4f}ms")

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
    return test_state.get_dict()

@app.post("/stop")
async def stop_test():
    test_state.stop_event.set()
    test_state.update(running=False)
    return {"status": "stopping"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
