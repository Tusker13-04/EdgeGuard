# Architecture Reference — EdgeGuard (feature/uno-q-migration)

**Date:** 2026-05-16  
**Status:** Current — reflects `feature/uno-q-migration` HEAD  
**Purpose:** Single-source-of-truth for all transport, module, thread, and data-flow
details. Use this document when verifying code behaviour or debugging the live system.

---

## 1. System Overview

EdgeGuard is a two-process, dual-core predictive maintenance pipeline:

```
STM32U585 (MCU)                QRB2210 MPU (Debian Linux)
│                              │
│  Zephyr RTOS acq thread      │  arduino-router daemon
│  ├─ k_sem ISR every 25 smp  │  └─ /var/run/arduino-router.sock  (AF_UNIX SOCK_STREAM)
│  └─ accumulate 4 ISRs        │       │
│  Bridge.notify(               │       │  msgpack-rpc notification
│    "sensor_batch",            │       │  [2, "sensor_batch", [<raw_bytes>]]
│    100×24=2400 bytes)  ──────►│       │
│                              │  BridgeReceiver.run() (Thread 1)
│  loop() — DS18B20 poll       │  ├─ BridgeParser.parse_batch_bytes()
│  IWatchdog.reload()          │  ├─ accel clip ±200 m/s², temp clamp -40..125 °C
                                │  └─ FastCircularBuffer.add_row()  (lock: brief)
                                │
                                │  PipelineEngine.run_inference_loop() (Main Thread)
                                │  ├─ FastCircularBuffer.get_snapshot()  (copy outside lock)
                                │  ├─ InferencePipeline.run_cycle()
                                │  │   ├─ ONNX: ort.InferenceSession (CPUExecutionProvider)
                                │  │   └─ RMS fallback (ONNX absent or > 5 failures)
                                │  └─ print(JSON) → stdout  @ 2 Hz
                                │
                                │  dashboard/server.py (uvicorn, separate process)
                                │  ├─ asyncio.create_subprocess_exec(main.py)
                                │  ├─ stdout readline → JSON validate
                                └─ WebSocket broadcast → browser clients
```

---

## 2. Module Inventory

### 2.1 Firmware (`firmware/uno_q_main/`)

| File | Role |
|---|---|
| `config.h` | Authoritative hardware constants: pins, `FIFO_WATERMARK=100`, `DS18B20_CONV_MS=750`, `IWDG_TIMEOUT_US=4000000` |
| `uno_q_main.ino` | Acquisition firmware: LIS3DH init, Zephyr `k_sem` ISR, batch loop, `Bridge.notify("sensor_batch", ...)`, DS18B20 async poll, IWatchdog |
| `platformio.ini` | PlatformIO build: `env:uno_q`, `board=nucleo_u575zi_q` (fallback), STM32 platform, lib deps |

**FIFO batching detail:**  
The LIS3DH FIFO is 32 levels. The hardware watermark register is set to **25** (5-bit max
is 31, register value `(0x01<<6) | 25`). The ISR fires every 25 samples. The acquisition
thread loops `FIFO_WATERMARK = 100` times per ISR, effectively draining across 4
consecutive ISR events per batch. `Bridge.notify` transmits `100 × 24 = 2 400` bytes
per call.

> **Known quirk in firmware comments:** `uno_q_main.ino` line comments mention
> "25 watermark and send every 4 interrupts" which is consistent with the above,
> but the outer loop `for (uint8_t i = 0; i < FIFO_WATERMARK; i++)` runs 100 times
> on every single ISR trigger, not every 4. This means one ISR drains the hardware
> FIFO 100 times, reading stale/repeated data for positions 26–100 when only 25
> samples are physically ready. **Practical impact:** the batch contains up to 75
> repeated samples per ISR. For anomaly detection this is tolerable (repeated samples
> do not introduce energy), but it is not ideal and should be addressed in a future
> firmware revision.

### 2.2 Python Pipeline (`src/`)

| Module | Key Exports | Notes |
|---|---|---|
| `schema.py` | `FEATURE_COLS`, `N_FEATURES=4`, `SAMPLE_RATE_HZ=400`, `WINDOW_SIZE=200`, `BATCH_PACKETS=100`, `PACKET_SIZE=24`, `BRIDGE_SOCK_PATH`, `BaseReceiver` ABC | Constants shared by all modules |
| `buffer.py` | `FastCircularBuffer`, `ACCEL_COLS`, `TEMP_COL` | Ring buffer; `total_written` is monotonically increasing, never saturates |
| `bridge_receiver.py` | `BridgeParser`, `BridgeReceiver` | Production ingest; Unix socket; `drop_rate_pct` uses sequence gap detection |
| `udp_receiver.py` | `PacketParser`, `UDPReceiver`, `parse_payload` | Legacy bench transport; binds `127.0.0.1` by default |
| `capture.py` | `record_session`, `slice_windows`, `save_window_as_csv` | Uses `buffer.total_written` watermark (fix #3/#12) |
| `engine.py` | `PipelineEngine`, `format_telemetry_json` | Orchestrates ingest thread + inference loop; propagates thread crashes via `_ingest_exc` |
| `inference.py` | `InferencePipeline`, `load_model`, `INFERENCE_INTERVAL_S=0.5` | ONNX circuit breaker at 5 consecutive failures; RMS fallback slices `[-WINDOW_SIZE:]` (fix #4) |

### 2.3 Dashboard (`dashboard/`)

| File | Role |
|---|---|
| `server.py` | FastAPI app; spawns `main.py` as asyncio subprocess; broadcasts stdout to WebSocket clients; auto-restarts on crash |
| `index.html` | Single-page operator + engineering UI; consumes `/ws` WebSocket |

---

## 3. Thread and Process Model

```
Process: main.py
  Thread: MainThread
    PipelineEngine.run_inference_loop()   <- yields telemetry dicts, prints JSON
  Thread: _ingest_thread (daemon)
    BridgeReceiver.run()                  <- blocks on unix socket, feeds buffer

Process: uvicorn (dashboard/server.py)
  asyncio event loop
    _pipeline_reader_with_restart()       <- subprocess: main.py stdout reader
    websocket_endpoint()                  <- per client coroutine
    _broadcast()                          <- fan-out to all WS clients
```

**Thread-safety invariants:**
- `FastCircularBuffer.add_row()` holds `_lock` for one numpy row write only.
- `FastCircularBuffer.get_snapshot()` holds `_lock` for scalar index copy only;
  the expensive `np.concatenate` runs outside the lock.
- `BridgeReceiver._handle_batch()` holds `_lock` for counter updates only;
  `buf.add_row()` is called outside the receiver lock.
- `stop_event` is a `threading.Event`; setting it from signal handler is safe.

---

## 4. Data Flow: Sample → Telemetry

```
1. LIS3DH hardware FIFO accumulates 25 samples → fires INT1 (RISING)
2. Zephyr ISR: k_sem_give(&fifo_sem)
3. acq_thread_func: k_sem_take → loop FIFO_WATERMARK(100) iterations
   → fills batch[100] SensorPayload array (32-byte aligned)
4. Bridge.notify("sensor_batch", batch, 2400)
5. arduino-router: forwards as msgpack notification [2, "sensor_batch", [bytes]]
6. BridgeReceiver: sock.recv → msgpack.Unpacker → _handle_batch(data)
7. BridgeParser.parse_batch_bytes(data):
   a. np.frombuffer → structured array (100 rows)
   b. clip accel to ±200 m/s²
   c. clamp temp to -40..125 °C (last-known substitution)
   d. returns list of (timestamp_us, sequence_id, features[4])
8. FastCircularBuffer.add_row(features)  → capacity 1600 (live) or duration×400 (capture)
9. InferencePipeline.run_cycle() at 2 Hz:
   a. buffer.get_snapshot() → ndarray shape (n, 4)
   b. if n < 200: return {"label": "buffering"}
   c. ONNX: x = snapshot[-200:][np.newaxis]  → shape (1,200,4)  → ort.run
   d. RMS fallback: rms = sqrt(mean(snapshot[-200:, 0:3]**2))  → score/threshold
10. PipelineEngine: build telemetry dict → print(JSON) → stdout
11. dashboard/server.py: readline → json.loads → WebSocket broadcast
```

---

## 5. Transport Socket Protocol

**Socket:** `AF_UNIX SOCK_STREAM` at `/var/run/arduino-router.sock`  
**Wire format:** msgpack-rpc notification (type = 2, no response expected)

```
[
  2,               // notification type (int)
  "sensor_batch",  // method name (str)
  [<raw_bytes>]    // params: list with one bytes element (2400 bytes)
]
```

`BridgeReceiver` uses `msgpack.Unpacker(raw=False)` in streaming mode over the
socket, allowing multiple notifications to arrive in one `recv()` call.

**Reconnect policy:** On `ConnectionRefusedError` or `socket.error`, sleep 2 s and
retry indefinitely. Intended for cases where `arduino-router` restarts.

---

## 6. Inference Pipeline Detail

### 6.1 ONNX Path

- Model file: `model/edgeguard.onnx` (gitignored; deploy after Edge Impulse training)
- Provider: `CPUExecutionProvider` (QRB2210 has no GPU)
- Input: `"input"`, shape `(1, 200, 4)`, `float32`
- Output: `"output"`, shape `(1, n_classes)`, `float32`
- Classes: `[0] = "normal"`, `[1] = "imbalance"`
- Circuit breaker: 5 consecutive failures → `self.sess = None` → permanent RMS fallback
  until process restart

### 6.2 RMS Fallback

```python
xyz   = snapshot[-200:, 0:3]          # (200, 3) — most recent 0.5 s
rms   = sqrt(mean(xyz ** 2))          # scalar
score = min(1.0, rms / 40.0)          # 40.0 m/s^2 threshold (~4 g)
label = "imbalance" if score > 0.5 else "normal"
```

`RMS_ANOMALY_THRESHOLD = 40.0 m/s²` is a calibration starting point, not a
validated value. Calibrate against baseline motor vibration before production use.

### 6.3 Telemetry Schema

```json
{
  "ts":             1747392000.123,   // Unix timestamp (float, 3dp)
  "label":          "normal",         // "normal" | "imbalance" | "buffering"
  "imbalance_prob": 0.02,             // float 0–1, 4dp
  "normal_prob":    0.98,             // float 0–1, 4dp
  "source":         "onnx",           // "onnx" | "rule_based" | "none"
  "latency_ms":     4.1,              // snapshot + inference time (ms)
  "drop_rate_pct":  0.0,              // cumulative packet drop %
  "n_rows":         200,              // rows used for this inference
  "board_temp_c":   27.4              // most recent DS18B20 reading (°C)
}
```

---

## 7. CLI Reference

### `main.py`

| Flag | Default | Description |
|---|---|---|
| `--mode` | `bridge` | `bridge` (Unix socket) or `udp` (bench only) |
| `--port` | `4444` | UDP port (UDP mode only) |
| `--bridge-fifo` | `$EDGEGUARD_BRIDGE_FIFO` or `/run/arduino/sensor_batch` | Override socket/FIFO path |
| `--interval` | `0.5` | Inference interval (seconds) |
| `--demo` | *(unset)* | Replay a `.jsonl` telemetry file |

### `capture_session.py`

| Flag | Default | Description |
|---|---|---|
| `--mode` | `bridge` | `bridge` or `udp` |
| `--label` | *(required)* | `normal` or `imbalance` |
| `--duration` | `30` | Recording duration (seconds) |
| `--out` | `data/raw` | Output directory |
| `--bridge-fifo` | *(unset)* | Override socket/FIFO path |

### `uvicorn dashboard.server:app`

| Env Var | Default | Description |
|---|---|---|
| `EDGEGUARD_MODE` | `bridge` | Mode passed to `main.py` subprocess |
| `EDGEGUARD_DEMO` | *(unset)* | Path to `.jsonl` replay file |
| `EDGEGUARD_BRIDGE_FIFO` | *(unset)* | Override socket path |
| `EDGEGUARD_MAX_CLIENTS` | `10` | Max simultaneous WebSocket clients |
| `EDGEGUARD_ALLOWED_ORIGINS` | *(any)* | Comma-separated origin allowlist |

---

## 8. Known Limitations and Open Issues

| Issue | Severity | Status |
|---|---|---|
| Firmware ISR loop drains 100 slots on every 25-sample IRQ; positions 26–100 may be repeated data | Medium | Open — tolerable for anomaly detection |
| `BridgeReceiver` depends on `arduino-router` external daemon (not in this repo) | High | Acceptable; daemon ships with UNO Q OS |
| ONNX model reload requires process restart (SIGHUP not implemented) | Low | Open |
| `RMS_ANOMALY_THRESHOLD = 40.0 m/s²` is not calibrated to any specific motor | Medium | Must calibrate before production |
| `msgpack` not listed in `requirements.txt` | Medium | Add `msgpack>=1.0.0` to `requirements.txt` |
| Python >= 3.10 required; QRB2210 default Python version not confirmed in docs | Low | Verify with `python3 --version` on board |

---

## 9. Dependency Versions

From `requirements.txt` (pinned, fix #13):

| Package | Version | Notes |
|---|---|---|
| `numpy` | 1.26.4 | numpy 2.0 has breaking API changes |
| `onnxruntime` | 1.17.3 | 1.18 breaks inference input handling |
| `fastapi` | 0.110.3 | |
| `uvicorn[standard]` | 0.29.0 | |
| `websockets` | 12.1 | |
| `msgpack` | *missing* | Required by `bridge_receiver.py` — add to requirements.txt |

Firmware library versions (from `firmware/platformio.ini`):

| Library | Version |
|---|---|
| `adafruit/Adafruit LIS3DH` | ^1.2.4 |
| `adafruit/Adafruit Unified Sensor` | ^1.1.14 |
| `paulstoffregen/OneWire` | ^2.3.8 |
| `milesburton/DallasTemperature` | ^3.11.0 |
| `Arduino_RouterBridge` | Manual install (not yet on PlatformIO registry) |
