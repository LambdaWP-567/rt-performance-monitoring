import sys
import os

# Add the project root to sys.path to support standalone execution
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import socket
import struct
import time
import logging
import threading
from typing import Optional
from src.metrics import get_meter

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("network_worker")

# Custom EtherType for our test
ETH_P_RT = 0x88B5 # Local Experimental EtherType

class NetworkWorker:
    def __init__(self, interface: str = "lo", interval_s: float = 1.0):
        self.interface = interface
        self.interval_s = interval_s
        self.running = False
        self.sock = None

        self.min_rtt = float('inf')
        self.max_rtt = float('-inf')
        self.total_rtt = 0
        self.count = 0

        self._setup_otel()

    def _setup_otel(self):
        try:
            meter = get_meter()
            self.otel_max_rtt = meter.create_gauge("net.rtt.max", unit="ms", description="Max network RTT")
            self.otel_min_rtt = meter.create_gauge("net.rtt.min", unit="ms", description="Min network RTT")
            self.otel_avg_rtt = meter.create_gauge("net.rtt.avg", unit="ms", description="Average network RTT")
        except Exception as e:
            logger.warning(f"OTel setup failed: {e}")

    def _setup_socket(self):
        if sys.platform == "linux":
            try:
                # AF_PACKET, SOCK_RAW
                self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_RT))
                self.sock.bind((self.interface, 0))
                self.sock.settimeout(0.5)
                logger.info(f"Opened raw socket on {self.interface} (Linux)")
            except PermissionError:
                logger.warning("Permission denied for raw socket. Using mock mode.")
                self.sock = None
            except Exception as e:
                logger.error(f"Failed to setup raw socket: {e}")
                self.sock = None
        else:
            logger.warning(f"Raw sockets not fully implemented for {sys.platform}. Using mock mode.")
            self.sock = None

    def run(self, duration_s: Optional[float] = None, stop_event: Optional[threading.Event] = None):
        self._setup_socket()
        self.running = True
        start_time = time.time()

        logger.info(f"Starting Network RTT test on {self.interface}")

        try:
            while self.running:
                if stop_event and stop_event.is_set():
                    self.running = False
                    break
                loop_start = time.time()

                if self.sock:
                    rtt = self._measure_real_rtt()
                else:
                    rtt = self._measure_mock_rtt()

                if rtt is not None:
                    self.min_rtt = min(self.min_rtt, rtt)
                    self.max_rtt = max(self.max_rtt, rtt)
                    self.total_rtt += rtt
                    self.count += 1

                    avg = self.total_rtt / self.count
                    if self.count % 10 == 0:
                        logger.info(f"Network RTT (ms): min={self.min_rtt:.4f}, max={self.max_rtt:.4f}, avg={avg:.4f}")
                        self._update_otel(avg)

                    # Update UI state for every sample (network tests are slower)
                    self._update_test_state(avg)

                sleep_time = self.interval_s - (time.time() - loop_start)
                if sleep_time > 0:
                    time.sleep(sleep_time)

                if duration_s and (time.time() - start_time) >= duration_s:
                    break
        except KeyboardInterrupt:
            self.running = False
        finally:
            if self.sock:
                self.sock.close()

        logger.info("Network RTT test stopped")
        self.report()

    def _update_test_state(self, avg):
        try:
            if hasattr(self, 'shared_res'):
                self.shared_res['avg'] = avg
                self.shared_res['max'] = self.max_rtt
                self.shared_res['min'] = self.min_rtt
        except Exception:
            pass

    def _update_otel(self, avg):
        try:
            self.otel_max_rtt.set(self.max_rtt)
            self.otel_min_rtt.set(self.min_rtt)
            self.otel_avg_rtt.set(avg)
        except Exception:
            pass

    def _measure_real_rtt(self) -> Optional[float]:
        # Construct a simple L2 frame
        # Dest MAC (6 bytes), Src MAC (6 bytes), EtherType (2 bytes), Payload
        # We use dummy MACs for local loopback test if interface is lo
        dst_mac = b'\xff\xff\xff\xff\xff\xff'
        src_mac = b'\x00\x00\x00\x00\x00\x00'
        payload = struct.pack("!Q", int(time.time_ns()))
        frame = dst_mac + src_mac + struct.pack("!H", ETH_P_RT) + payload

        try:
            t_send = time.perf_counter()
            self.sock.send(frame)

            data = self.sock.recv(2048)
            t_recv = time.perf_counter()

            return (t_recv - t_send) * 1000 # to ms
        except socket.timeout:
            return None
        except Exception as e:
            logger.debug(f"Send/Recv error: {e}")
            return None

    def _measure_mock_rtt(self) -> float:
        # Simulate a network latency between 0.1 and 2ms
        import random
        base = 0.5
        jitter = random.uniform(-0.1, 0.5)
        return base + jitter

    def report(self):
        if self.count > 0:
            avg = self.total_rtt / self.count
            print(f"\n--- Network Results ---")
            print(f"Samples: {self.count}")
            print(f"Min RTT: {self.min_rtt:.4f} ms")
            print(f"Max RTT: {self.max_rtt:.4f} ms")
            print(f"Avg RTT: {avg:.4f} ms")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", type=str, default="lo", help="Network interface")
    parser.add_argument("--interval", type=float, default=1.0, help="Interval in seconds")
    parser.add_argument("--duration", type=float, default=5.0, help="Duration in seconds")
    args = parser.parse_args()

    worker = NetworkWorker(interface=args.interface, interval_s=args.interval)
    worker.run(duration_s=args.duration)
