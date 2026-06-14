# EdgeGuard

> Industrial predictive maintenance node — Arduino UNO Q (STM32U585 + QRB2210)

EdgeGuard detects mechanical anomalies (imbalance, abnormal vibration) in rotating
machinery using a low-cost sensor node streaming to an integrated MPU for real-time
ML inference. Targeted for the **Arduino UNO Q** platform.

---

## Architecture

EdgeGuard runs on the **Arduino UNO Q**, leveraging its dual-core architecture:

1. **STM32U585 MCU** — Real-time sensor acquisition at 400 Hz, 100-sample FIFO batching.
2. **QRB2210 MPU** — Python 3.10+ pipeline: ingest, circular buffer, ONNX/RMS inference, dashboard.

Communication between the cores uses **Arduino_RouterBridge** (`Bridge.notify`) with
**MessagePack** serialisation over the internal high-speed UART. The `arduino-router`
daemon on the MPU side exposes a **Unix Domain Socket** at `/var/run/arduino-router.sock`
using the standard msgpack-rpc notification format `[2, method, params]`.

```
┌─────────────────────────────┐  Arduino_RouterBridge   ┌──────────────────────────────────┐
│  Arduino UNO Q (MCU)        │  MessagePack / UART  ►  │  Arduino UNO Q (MPU)             │
│  STM32U585 @ 160 MHz        │                          │  QRB2210 · Debian Linux          │
│                             │                          │                                  │
│  LIS3DH  I²C 400 kHz        │                          │  arduino-router daemon           │
│  ├─ 3-axis accel @ 400 Hz   │                          │  └─ /var/run/arduino-router.sock │
│  ├─ ±8 g range              │                          │                                  │
│  └─ FIFO watermark 25 smp   │                          │  Thread 1 (BridgeReceiver)       │
│                             │                          │  ├─ Unix socket ingest           │
│  DS18B20  1-Wire async      │                          │  └─ FastCircularBuffer.add_row() │
│  └─ board temp ±0.5 °C      │                          │                                  │
│                             │                          │  Thread 2 (InferencePipeline)    │
│  Zephyr RTOS acq thread     │                          │  ├─ 2 Hz inference loop          │
│  k_sem ISR → batch loop     │                          │  ├─ ONNX model (edgeguard.onnx)  │
│  IWatchdog 4 s timeout      │                          │  └─ RMS fallback (no model)      │
└─────────────────────────────┘                          │                                  │
                                                         │  FastAPI dashboard :8080         │
                                                         │  ├─ WebSocket broadcast          │
                                                         └─ main.py subprocess stdout ─────┘
```

### Thread-Safety Design

| Layer | Mechanism | Details |
|---|---|---|
| `FastCircularBuffer.add_row()` | `threading.Lock` | Held briefly per row write |
| `FastCircularBuffer.get_snapshot()` | `threading.Lock` | Scalar indices copied under lock; `np.concatenate` executes outside lock to prevent ingest starvation |
| `BridgeReceiver._handle_batch()` | `threading.Lock` | Single acquisition per batch for all counter/state updates |
| Firmware acquisition thread | Zephyr counting semaphore | `k_sem_init(&fifo_sem, 0, 4)` — buffers up to 4 pending ISR signals, preventing IRQ loss under CPU load |
| Firmware watchdog | IWDG reloaded by `acq_thread_func` | Reloaded after every 100-sample batch (~250 ms); `loop()` starvation deliberately triggers reset |

**Payload struct** (24 bytes, packed — must match `src/schema.py`):
```c
struct __attribute__((packed)) SensorPayload {
    uint32_t timestamp_us;   // μs since MCU boot
    uint32_t sequence_id;    // monotonic counter (drop detection)
    float    accel_x;        // m/s²  (LIS3DH ±8 g)
    float    accel_y;        // m/s²
    float    accel_z;        // m/s²
    float    board_temp;     // °C   (DS18B20 ±0.5 °C)
};
static_assert(sizeof(SensorPayload) == 24);
```

**Batch constants** (authoritative sources shown):

| Constant | Value | Defined in |
|---|---|---|
| SAMPLES_PER_IRQ | 25 samples | `firmware/uno_q_main/config.h` — hardware FIFO watermark register value; ISR fires every 25 samples |
| FIFO_WATERMARK | 100 samples | `firmware/uno_q_main/config.h` — firmware batch accumulation target (4 × SAMPLES_PER_IRQ); **not** the hardware FIFO depth |
| BATCH_PACKETS | 100 packets | `src/schema.py` |
| BATCH_SIZE | 2 400 bytes | `src/schema.py` (100 × 24) |
| SAMPLE_RATE_HZ | 400 Hz | `src/schema.py` |
| WINDOW_SIZE | 200 samples (0.5 s) | `src/schema.py` |
| INFERENCE_INTERVAL_S | 0.5 s (2 Hz) | `src/inference.py` |

> **Note on FIFO strategy:** The LIS3DH hardware FIFO is 32 levels deep.
> `SAMPLES_PER_IRQ = 25` is the value written to the hardware watermark register —
> the ISR fires every 25 samples. `FIFO_WATERMARK = 100` is a **firmware-side batch
> accumulation target**: the acquisition thread counts 4 ISR events (4 × 25 = 100
> samples) before calling `Bridge.notify`. These two constants serve different
> purposes; `FIFO_WATERMARK` does **not** represent the hardware FIFO depth or the
> hardware watermark level.

---

## Hardware

| Component | Role |
|---|---|
| Arduino UNO Q | Integrated MCU (STM32U585 @ 160 MHz) + MPU (QRB2210 Debian Linux) |
| Adafruit LIS3DH STEMMA QT | 3-axis accelerometer, I²C (0x18 on Wire1/Qwiic), 400 Hz ODR, ±8 g range |
| DS18B20 waterproof probe | Motor-case temperature, 1-Wire on D4, ±0.5 °C, 12-bit async conversion |

**UNO Q Pin Mapping** (authoritative: `firmware/uno_q_main/config.h`):

| Function | UNO Q Pin | Notes |
|---|---|---|
| I2C SDA | SDA / Qwiic (Wire1) | LIS3DH data — use Qwiic cable |
| I2C SCL | SCL / Qwiic (Wire1) | LIS3DH clock |
| LIS3DH INT1 | D2 | FIFO watermark interrupt (EXTI, RISING) |
| DS18B20 DATA | D4 | 1-Wire (3.3 V, 4.7 kΩ pull-up to 3.3 V) |

---

## Quickstart

### 1. MCU Firmware (STM32U585)

**Option A — PlatformIO (recommended):**
```bash
cd EdgeGuard
pio run -e uno_q --target upload
# Board: nucleo_u575zi_q (fallback until STM32U585AI-UNO is published in registry)
# Install Arduino_RouterBridge manually — see firmware/platformio.ini for link
```

**Option B — Arduino IDE:**
1. Open `firmware/uno_q_main/uno_q_main.ino`.
2. Select **Arduino UNO Q** board.
3. Install required libraries: `Adafruit LIS3DH`, `Adafruit Unified Sensor`,
   `OneWire`, `DallasTemperature`, `Arduino_RouterBridge`.
4. Upload.

### 2. MPU Setup (QRB2210) & Greengrass V2

Connect via SSH or the UNO Q serial console.

```bash
git clone https://github.com/Tusker13-04/EdgeGuard
cd EdgeGuard
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**AWS Greengrass Integration:**
EdgeGuard runs as an AWS IoT Greengrass v2 component (`com.edgeguard.inference`).
The provided `recipe.yaml` manages the Python lifecycle and grants permissions for IPC MQTT publishing and local shadow subscriptions.

1. Ensure the `arduino-router` daemon is running.
2. Install Greengrass Core on the Debian OS with a proper Token Exchange Service (TES) role.
3. Deploy the component via the AWS IoT Console using the local `recipe.yaml`.

### 3. Live inference

```bash
# Bridge IPC mode — production default (UNO Q)
python main.py

# Override socket path if arduino-router uses a non-default location
python main.py --bridge-fifo /run/arduino/sensor_batch

# Legacy UDP mode — bench/dev without physical UNO Q hardware
python main.py --mode udp --port 4444

# Replay a recorded telemetry file (dashboard demo)
python main.py --demo data/demo.jsonl
```

Telemetry is emitted as one JSON line per inference cycle to stdout:
```json
{"ts": 1747392000.123, "label": "normal", "imbalance_prob": 0.02,
 "normal_prob": 0.98, "source": "onnx", "latency_ms": 4.1,
 "drop_rate_pct": 0.0, "n_rows": 200, "board_temp_c": 27.4}
```

`source` is `"onnx"` when the model is loaded, `"rule_based"` when running the
RMS fallback, and `"none"` while the buffer is filling on startup.

### 4. Capture training data

```bash
# Bridge IPC (UNO Q production)
python capture_session.py --label normal    --duration 30
python capture_session.py --label imbalance --duration 30

# UDP bench mode (without UNO Q hardware)
python capture_session.py --mode udp --label normal    --duration 30
python capture_session.py --mode udp --label imbalance --duration 30

# Output: data/raw/normal/*.csv and data/raw/imbalance/*.csv
# Upload these to Edge Impulse for training.
```

The buffer uses a **monotonic `total_written` counter** as the recording-start
watermark, so the captured window is always exactly `duration_s × 400` rows,
regardless of how many times the 4-second ring has wrapped before the session starts.

### 5. Dashboard

```bash
uvicorn dashboard.server:app --host 0.0.0.0 --port 8080
# Open http://<uno-q-ip>:8080
```

**Dashboard environment variables:**

| Variable | Default | Description |
|---|---|---|
| `EDGEGUARD_MODE` | `bridge` | Ingest mode passed to `main.py` (`bridge` or `udp`) |
| `EDGEGUARD_DEMO` | *(unset)* | Path to `.jsonl` replay file; activates demo mode |
| `EDGEGUARD_BRIDGE_FIFO` | *(unset)* | Override Bridge IPC socket path |
| `EDGEGUARD_MAX_CLIENTS` | `10` | Max simultaneous WebSocket clients |
| `EDGEGUARD_ALLOWED_ORIGINS` | *(any)* | Comma-separated allowed WebSocket origins |
| `EDGEGUARD_RMS_THRESHOLD` | `40.0` | RMS anomaly threshold in m/s². Override to calibrate for your specific motor. |

### 6. Deploy ONNX model via Over-The-Air (OTA) Updates

No manual copying is needed in production. Once your Edge Impulse model is trained:
1. Export the **ONNX** model and upload it to your designated S3 bucket (e.g. `s3://edgeguard-artifacts/models/v1.2.onnx`).
2. Update the `desired` state of the `EdgeGuardModelShadow` device shadow in AWS IoT Core:
   ```json
   { "state": { "desired": { "model_version": "models/v1.2.onnx" } } }
   ```
3. The Greengrass IPC client (`src/engine.py`) detects the delta, securely downloads the new model using AWS TES credentials via `boto3`, and hot-reloads the inference session seamlessly.

*(Fallback)*: If you are running locally without Greengrass:
```bash
mkdir -p model
cp edgeguard.onnx model/edgeguard.onnx
```
The pipeline falls back to RMS-based anomaly detection until a model is loaded.

---

## Edge Impulse Training

Training is done in **Edge Impulse Studio** — no separate training script.

1. Upload CSVs from `data/raw/normal/` and `data/raw/imbalance/` to your EI project.
2. Create a **Spectral Analysis** DSP block (time + low-frequency spectral features).
3. Train a **1D CNN classifier** or **anomaly detection** block.
4. Export → **ONNX** or **C++ library**.
5. Copy `edgeguard.onnx` → `model/edgeguard.onnx` (step 6 above).

**ONNX model contract:**

| Slot | Name | Shape | dtype |
|---|---|---|---|
| Input | `input` | `(1, 200, 4)` | float32 |
| Output | `output` | `(1, n_classes)` | float32 |

Class order: `[0] = "normal"`, `[1] = "imbalance"`.

---

## Data Format (Edge Impulse CSV)

Files produced by `capture_session.py` are Edge Impulse-ready:

```
timestamp,accel_x,accel_y,accel_z,board_temp
0.0,0.123,-0.045,9.801,25.0
2.5,0.131,-0.042,9.798,25.0
5.0,...
```

- Timestamp in **milliseconds**, 2.5 ms intervals (400 Hz → `ROW_INTERVAL_MS = 2.5`)
- Edge Impulse infers sampling frequency from timestamp deltas
- Label encoded in folder name (`normal/`, `imbalance/`)
- One file = one 200-sample / 0.5-second window

---

## Repository Structure

```
EdgeGuard/
├── firmware/
│   ├── platformio.ini             # PlatformIO build (env:uno_q)
│   └── uno_q_main/
│       ├── config.h               # Hardware constants (SAMPLES_PER_IRQ, FIFO_WATERMARK, pins)
│       └── uno_q_main.ino         # STM32U585 acquisition firmware (Zephyr RTOS)
├── src/                           # MPU Python pipeline
│   ├── schema.py                  # Shared constants + BaseReceiver ABC
│   ├── bridge_receiver.py         # Unix socket ingest (production)
│   ├── udp_receiver.py            # UDP ingest (bench/dev only)
│   ├── buffer.py                  # FastCircularBuffer (numpy, thread-safe)
│   ├── capture.py                 # Window slicing + Edge Impulse CSV writer
│   ├── engine.py                  # PipelineEngine orchestration
│   └── inference.py               # ONNX / RMS inference pipeline
├── dashboard/
│   ├── index.html                 # Operator + Engineering UI
│   └── server.py                  # FastAPI + WebSocket broadcast server
├── tests/
│   ├── test_pipeline.py
│   ├── test_buffer.py
│   ├── test_capture.py
│   ├── test_inference.py
│   ├── test_bridge_receiver.py
│   ├── test_udp_receiver.py
│   └── test_cli.py                # CLI argument validation (ISS-05)
├── data/
│   └── raw/                       # Captured CSVs (gitignored contents)
│       ├── normal/
│       └── imbalance/
├── model/
│   └── edgeguard.onnx             # ← ADD after EI training (gitignored)
├── capture_session.py             # CLI for labelled data capture sessions
├── main.py                        # CLI entry point for live inference
├── requirements.txt               # Pinned Python dependencies
└── README.md
```

---

## Running Tests

```bash
pip install pytest
pytest tests/ -v
```

---

## Limitations

- **RMS threshold** (`EDGEGUARD_RMS_THRESHOLD`, default `40.0 m/s²` / ~4 g) is a
  calibration starting point. Set via environment variable and calibrate against
  baseline vibration for your specific motor before relying on the rule-based fallback
  in production.
- **DS18B20** accurate to ±0.5 °C; used for motor-case thermal trend only, not
  precision measurement.
- **ONNX model slot** is wired but empty until Edge Impulse training is complete.
  The rule-based RMS fallback runs automatically until then. After ONNX failures
  exceed 5 consecutive cycles, the session is permanently disabled until process
  restart. After deployment, restart `main.py` to load the new model — live SIGHUP
  reload is not implemented.
- **arduino-router dependency**: `BridgeReceiver` requires the `arduino-router` daemon
  to be running on the MPU and listening at `/var/run/arduino-router.sock`. If the
  daemon is not active, the receiver will log connection errors and retry every 2 s.
  The daemon's behaviour when multiple clients connect simultaneously (e.g. running
  `main.py` and `capture_session.py` at the same time) is not verifiable from this
  codebase — its `accept()` loop is in the closed `arduino-router` binary. Until
  confirmed otherwise, treat the socket as **single-client**: stop `main.py` before
  running a capture session, or vice versa.
- **Python >= 3.10** required on the QRB2210 MPU. Confirm with `python3 --version`
  before running.
- **UDP mode** binds to `127.0.0.1` by default. Pass `bind_host=""` only when
  receiving packets from external hardware (e.g. bench ESP8266).
