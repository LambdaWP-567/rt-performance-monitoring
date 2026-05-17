# Real-Time Performance Monitor

This application continuously measures the real-time performance of the operating system, focusing on CPU scheduling jitter and Layer 2 network latency. It is designed to evaluate if a system is "RT-Ready" for industrial automation solutions.

## Key Features

- **CPU Jitter Measurement**: High-precision monitoring of wake-up latencies using `nanosleep` and monotonic clocks.
- **CPU Isolation/Affinity**: Automatically pins measurement threads to dedicated cores to avoid interference.
- **Layer 2 Network Latency**: Measures RTT using raw Ethernet frames (EtherType 0x88B5), bypassing the IP stack.
- **OpenTelemetry Integration**: Exports performance metrics to any OTel-compatible collector.
- **Web Interface**: FastAPI-based REST API for managing test cycles and monitoring status.
- **RT-Ready Judgment**: Automatic classification of the system based on deterministic performance thresholds.

## Requirements

- **Linux**: Recommended for real-time testing (ideally with `PREEMPT_RT` patch).
- **macOS**: Supported for development and relative performance measurement.
- **Root/Admin Privileges**: Required for CPU affinity settings and Raw Socket (L2) network access.

## Installation

```bash
pip install -r requirements.txt
```

## Running the Application

### Using Docker (Recommended)

```bash
# Build the container
docker build -t rt-monitor .

# Run the container (requires host network and privileges for raw sockets)
docker run --privileged --network host rt-monitor
```

### Manual Execution

```bash
export PYTHONPATH=$PYTHONPATH:.
python src/app.py
```

## Configuration

The application can be configured using environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `ENABLE_NET_TEST` | Set to `true` to enable Layer 2 network tests. | `false` |
| `NET_INTERFACE` | The network interface to use for L2 tests. | `lo` |

Example:
```bash
ENABLE_NET_TEST=true NET_INTERFACE=eth0 python src/app.py
```

## How to Run a Test

1. **Start a Test Cycle**:
   Send a POST request to `/start`. The default duration is 15 minutes.
   ```bash
   curl -X POST "http://localhost:8000/start" -H "Content-Type: application/json" -d '{"duration_min": 15.0, "cyclic": false, "core": 1}'
   ```

2. **Check Status & Results**:
   ```bash
   curl "http://localhost:8000/status"
   ```

3. **Stop Test Early**:
   ```bash
   curl -X POST "http://localhost:8000/stop"
   ```

## Judging "RT-Ready" Status

The application judges a system as **RT-Ready** if the following criteria are met:

- **Max CPU Jitter < 100 microseconds (100,000 ns)**:
  In industrial automation, deterministic behavior is critical. If the operating system's scheduler introduces more than 100µs of delay for a high-priority task, it is generally considered unsuitable for hard real-time tasks (like motion control or fast PLC cycles).

- **Consistent Network RTT**:
  While network RTT depends on the infrastructure, a real-time system should show minimal variance (jitter) in L2 reflections.

### Expected Results

- **Linux (PREEMPT_RT)**: Should pass with jitter often < 20µs.
- **Standard Linux/macOS**: Likely to fail (Jitter often > 500µs due to non-preemptive kernel tasks).

## Architecture

- `src/cpu_worker.py`: Core logic for CPU jitter measurement and affinity.
- `src/network_worker.py`: Layer 2 raw socket implementation.
- `src/metrics.py`: OpenTelemetry configuration.
- `src/app.py`: FastAPI web server and orchestration.
