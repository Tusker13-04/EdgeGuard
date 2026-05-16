# Design Spec: EdgeGuard Migration to Arduino UNO Q

**Date:** 2026-05-13  
**Updated:** 2026-05-16  
**Status:** Implemented  
**Branch:** `feature/uno-q-migration`  
**Target:** Arduino UNO Q (STM32U585 MCU + QRB2210 MPU)

---

## 1. Executive Summary

Migrate the EdgeGuard predictive maintenance prototype from ESP8266 + Raspberry Pi
to the integrated Arduino UNO Q platform. This move consolidates real-time sensor
acquisition (STM32U585 MCU) and high-level ML inference (QRB2210 MPU) onto a single
dual-core board, replacing the Wi-Fi/UDP transport with internal Bridge RPC over
high-speed UART, exposed to user Python code via a Unix Domain Socket.

---

## 2. Architecture

### 2.1 MCU (STM32U585 @ 160 MHz)

- **Framework:** Arduino Framework (STM32duino / STM32 Core), Zephyr RTOS primitives.
- **Build system:** PlatformIO `env:uno_q` (see `firmware/platformio.ini`). Arduino IDE supported as fallback.
- **Sensors:**
  - **LIS3DH (Accelerometer):** I²C on Wire1 (Qwiic), address `0x18`, 400 Hz ODR, ±8 g range.
    FIFO configured in Stream Mode with a 25-sample watermark. The ISR fires every 25
    samples; the acquisition thread accumulates **4 ISR events** before transmitting a
    **100-sample batch** (`FIFO_WATERMARK = 100` in `config.h`).
  - **DS18B20 (Temperature):** 1-Wire on D4 (3.3 V, 4.7 kΩ pull-up). Asynchronous 12-bit
    conversion (`DS18B20_CONV_MS = 750 ms`). Updated in `loop()`; latest value stamped into
    each `SensorPayload`.
- **Transport:** `Arduino_RouterBridge` — `Bridge.notify("sensor_batch", payload, size)`.
  Bridge method name is `sensor_batch` (not `sensor_data` as in original spec).
- **Watchdog:** `IWatchdog` with 4-second timeout (`IWDG_TIMEOUT_US = 4000000`).
  Reloaded in `loop()` every ~10 ms.

### 2.2 MPU (QRB2210 · Debian Linux)

- **Language:** Python 3.10+ (enforced by `sys.version_info` guard in `main.py`).
- **Transport daemon:** `arduino-router` exposes a Unix Domain Socket at
  `/var/run/arduino-router.sock` using msgpack-rpc notification format:
  `[2, "sensor_batch", [<raw_bytes>]]`.
- **Module breakdown:**

| Module | Role |
|---|---|
| `src/schema.py` | Shared constants (`FEATURE_COLS`, `WINDOW_SIZE`, `BRIDGE_SOCK_PATH`) + `BaseReceiver` ABC |
| `src/bridge_receiver.py` | Unix socket ingest — connects to `arduino-router.sock`, parses MsgPack batches |
| `src/udp_receiver.py` | Legacy UDP ingest — bench/dev only, binds to `127.0.0.1` by default |
| `src/buffer.py` | `FastCircularBuffer` — pre-allocated numpy ring, `total_written` monotonic counter |
| `src/capture.py` | `record_session()` — watermark-safe window capture, Edge Impulse CSV writer |
| `src/engine.py` | `PipelineEngine` — wires receiver + buffer + inference, manages threads |
| `src/inference.py` | `InferencePipeline` — ONNX + RMS fallback with failure-count circuit breaker |
| `dashboard/server.py` | FastAPI + WebSocket server; spawns `main.py` subprocess, broadcasts JSON |

---

## 3. Data Contract

The 24-byte `SensorPayload` struct is shared between firmware and Python.
Layout must be identical in `firmware/uno_q_main/uno_q_main.ino` and `src/schema.py`.

| Offset | Type | Field | Description |
|---|---|---|---|
| 0 | `uint32_t` | `timestamp_us` | Microseconds since MCU boot |
| 4 | `uint32_t` | `sequence_id` | Monotonic counter for drop detection |
| 8 | `float32` | `accel_x` | X-axis acceleration (m/s²) |
| 12 | `float32` | `accel_y` | Y-axis acceleration (m/s²) |
| 16 | `float32` | `accel_z` | Z-axis acceleration (m/s²) |
| 20 | `float32` | `board_temp` | Board/motor temperature (°C) |

`static_assert(sizeof(SensorPayload) == 24)` is enforced in firmware.
Python side: `np.dtype` with `<u4, <u4, <f4, <f4, <f4, <f4` (little-endian packed).

**Batch constants:**

| Constant | Value | Source |
|---|---|---|
| `FIFO_WATERMARK` | 100 | `config.h` (authoritative) |
| `BATCH_PACKETS` | 100 | `src/schema.py` |
| `BATCH_SIZE` | 2 400 bytes | `src/schema.py` (100 × 24) |
| `PACKET_SIZE` | 24 bytes | `src/schema.py` |

---

## 4. Hardware Mapping

Authoritative source: `firmware/uno_q_main/config.h`.

| Function | UNO Q Pin | Constant | Notes |
|---|---|---|---|
| I²C SDA | SDA / Qwiic | `LIS3DH_WIRE = Wire1` | Qwiic cable recommended |
| I²C SCL | SCL / Qwiic | `LIS3DH_WIRE = Wire1` | |
| LIS3DH I²C addr | — | `LIS3DH_ADDR = 0x18` | ADDR pin to GND |
| LIS3DH INT1 | D2 | `LIS3DH_INT1_PIN = 2` | EXTI, RISING, FIFO watermark |
| DS18B20 DATA | D4 | `ONE_WIRE_PIN = 4` | 4.7 kΩ pull-up to 3.3 V |

---

## 5. Key Bug Fixes Implemented

The following bugs were identified and fixed during this migration. Each is documented
in its source file with a `FIX #N` comment.

| Fix | File | Root Cause | Resolution |
|---|---|---|---|
| #2 | `src/buffer.py`, `capture_session.py` | `n_rows` saturates at 1 600 after 4 s; recording watermark always 0 | Added `total_written` monotonic counter to `FastCircularBuffer` |
| #3/#12 | `src/capture.py` | Recording start watermark used `buffer.n_rows` producing 0–4 windows instead of 60 | Use `buffer.total_written` as watermark |
| #4 | `src/inference.py` | RMS computed over full 4 s buffer history, diluting spike below threshold | Slice `snapshot[-WINDOW_SIZE:]` for RMS — same 0.5 s window as ONNX |
| #6 | `src/udp_receiver.py` | `UDPReceiver` bound to `0.0.0.0`, accepting packets from any host | Default `bind_host = "127.0.0.1"`; LAN access requires explicit opt-in |
| #13 | `requirements.txt` | Unpinned deps; `numpy 2.0` and `onnxruntime 1.18` break the pipeline | Pin: `numpy==1.26.4`, `onnxruntime==1.17.3` |
| server | `dashboard/server.py` | Default mode was `udp` — spawned `main.py --mode udp` even on live UNO Q | Changed default `_MODE` to `bridge` to match `main.py` |

---

## 6. Success Criteria

- **Connectivity:** `BridgeReceiver` connects to `/var/run/arduino-router.sock` within 2 s of `main.py` start.
- **Throughput:** 400 Hz effective sample rate with < 1% packet drop under normal operating conditions.
- **Latency:** End-to-end (sensor event → Python inference output) < 500 ms (one inference window).
- **Reliability:** Ingest thread crash propagates to `stop_event` via `PipelineEngine._ingest_exc`; process exits cleanly.
- **Capture:** `capture_session.py --duration 30` produces exactly 60 windows (12 000 rows / 200) with no silent truncation.
- **Fallback:** ONNX load failure produces `rule_based` telemetry within one inference cycle, not a crash.
- **Dashboard:** Operator view shows live `label`, `imbalance_prob`, `board_temp_c`, `drop_rate_pct` within one second of `uvicorn` start.
