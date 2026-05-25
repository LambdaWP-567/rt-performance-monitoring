# RT Performance Monitor

Measures CPU scheduling jitter and Layer 2 network latency to determine whether a system is suitable for real-time workloads such as industrial automation, motion control, or fast PLC cycles.

## Features

| Feature | Details |
|---|---|
| CPU jitter measurement | `nanosleep` + `CLOCK_MONOTONIC` at nanosecond resolution |
| SCHED_FIFO elevation | Measurement thread runs at RT scheduler priority 99 (requires root) |
| GC isolation | Python GC frozen and disabled during the hot loop |
| CPU affinity | Pins the measurement thread to a dedicated core |
| L2 network latency | Raw Ethernet frames (EtherType `0x88B5`), bypasses the IP stack |
| Adjustable threshold | RT verdict threshold configurable at runtime via UI or API |
| Live RT verdict | Judgment updates every 100 samples during a running test |
| OpenTelemetry | Exports metrics to any OTLP-compatible collector |
| Web UI | FastAPI dashboard with start / stop / reset controls |
| Headless / script mode | `--output file.json` for CI, Docker, or automated benchmarks |
| Debug logging | `--debug` flag for verbose output; silent HTTP access log by default |

## Requirements

- **Linux** — required for SCHED_FIFO, CPU affinity, and raw sockets. A `PREEMPT_RT`-patched kernel is recommended for hard real-time assessment.
- **macOS** — supported for development and relative comparison only.
- **Root / sudo** — required for `SCHED_FIFO` scheduling and raw socket access. Without root the measurement thread runs at normal priority and results will be pessimistic.

## Installation

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Running

### Web UI

```bash
# Normal (minimal logging)
sudo venv/bin/python src/app.py

# Verbose (shows per-request logs, jitter samples, scheduler details)
sudo venv/bin/python src/app.py --debug
```

Open **http://localhost:8000** in a browser. The port auto-increments if 8000 is busy; the chosen port is printed at startup. Override with `APP_PORT=8080`.

### Docker

```bash
docker build -t rt-monitor .

# Web UI
docker run --rm --privileged --network host rt-monitor

# Headless test with output file
docker run --rm --privileged --network host \
  -v $(pwd)/results:/output \
  rt-monitor \
  python src/cpu_worker.py \
    --duration 60 --core 1 --label docker \
    --output /output/docker.json
```

`--privileged` is required for `SCHED_FIFO` and raw sockets. Without it the test still runs but scheduler elevation will fail and jitter results will be inflated.

### Headless / script mode

Runs the CPU jitter test without the web server. Useful for automated benchmarks, CI pipelines, and side-by-side comparisons.

```bash
sudo venv/bin/python src/cpu_worker.py \
  --duration 60 \   # seconds
  --core 1 \        # CPU core to pin to
  --threshold 100000 \  # RT threshold in ns (default 100 µs)
  --label native \  # tag written into the JSON output
  --output results.json

# Print JSON to stdout
sudo venv/bin/python src/cpu_worker.py --duration 60 --output -
```

**Output schema:**
```json
{
  "label": "native",
  "version": "1.0.3",
  "timestamp": "2026-05-25T19:03:38Z",
  "platform": "linux",
  "duration_s": 60,
  "interval_ms": 1.0,
  "core": 1,
  "rt_threshold_ns": 100000,
  "samples": 60000,
  "jitter_ns": {
    "min": 3090,
    "max": 6048298,
    "avg": 7603.31,
    "p50": 5477,
    "p95": 15961,
    "p99": 27042,
    "p99_9": 49764
  },
  "rt_ready": false
}
```

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `ENABLE_NET_TEST` | Enable Layer 2 network latency test | `false` |
| `NET_INTERFACE` | Interface for L2 test | `lo` |
| `APP_PORT` | Force a specific web UI port | auto (8000–8009) |

## Web API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/status` | Current test state, live results, version |
| `POST` | `/start` | Start a test cycle (see body schema below) |
| `POST` | `/stop` | Signal a running test to stop |
| `POST` | `/reset` | Clear results (only when not running) |
| `GET` | `/config` | Current RT threshold |
| `POST` | `/config` | Update RT threshold (takes effect on next test start) |

**`/start` body:**
```json
{
  "duration_min": 15.0,
  "interval_cpu_ms": 1.0,
  "core": 1,
  "cyclic": false
}
```

**`/config` body:**
```json
{ "cpu_jitter_threshold_us": 100.0 }
```

**Cyclic mode:** when `cyclic: true` the test restarts automatically after each cycle and runs indefinitely. Each new cycle overwrites the previous results. Stop manually via `/stop` or the UI.

## RT-Readiness judgment

A system is judged **RT-Ready** if *every single* measured wake-up jitter is below the threshold. One outlier fails the test. The default threshold is **100 µs (100,000 ns)** — a widely-used boundary for soft real-time industrial tasks.

The threshold is adjustable at runtime via the UI slider or `POST /config`. Changes take effect on the next test start.

| Typical max jitter | Expected result |
|---|---|
| < 20 µs | `PREEMPT_RT` patched kernel — hard real-time |
| < 100 µs | RT-capable — default threshold |
| < 500 µs | Standard Linux — non-RT use only |
| > 500 µs | High jitter — VM, power-saving, heavy load |

See [FINDINGS.md](FINDINGS.md) for measured results on a Raspberry Pi 5 and a discussion of measurement accuracy vs a native C implementation.

## Architecture

```
src/
├── app.py            FastAPI server, test orchestration, API endpoints
├── cpu_worker.py     Jitter measurement loop (SCHED_FIFO, affinity, GC isolation)
├── network_worker.py L2 raw-socket RTT measurement
├── metrics.py        OpenTelemetry meter setup
├── version.py        Single source of version string
└── static/
    └── index.html    Web UI
```

### Measurement design

1. **Scheduler elevation** — `sched_setscheduler(SCHED_FIFO, 99)` ensures the OS treats the thread as an RT task. Without this, results reflect `SCHED_OTHER` latency, not RT capability.
2. **GC isolation** — `gc.freeze()` + `gc.disable()` before the hot loop; `gc.enable()` + `gc.collect()` after. Prevents Python's cyclic garbage collector from pausing the loop.
3. **Pre-allocated Timespec** — the `Timespec` struct passed to `nanosleep` is allocated once in `__init__` and reused every iteration, eliminating per-call heap allocation.
4. **Absolute-time accounting** — `next_target += interval_ns` (not `now + interval_ns`) prevents drift accumulation across thousands of iterations.
5. **Frame validation** — the L2 network test embeds a sequence number in each frame and drains the receive queue until it sees its own echo, preventing false RTT readings from stray frames.
