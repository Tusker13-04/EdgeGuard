# EdgeGuard

> Industrial predictive maintenance node — Arduino UNO Q (STM32 + QRB2210)

EdgeGuard detects mechanical anomalies (imbalance, abnormal vibration) in rotating
machinery using a low-cost sensor node streaming to an integrated MPU for real-time
ML inference. Targeted for the Arduino UNO Q platform.

---

## Architecture

EdgeGuard runs on the **Arduino UNO Q**, leveraging its dual-core architecture:
1. **STM32U585 MCU:** Real-time sensor acquisition and batching.
2. **QRB2210 MPU:** High-level Python pipeline, ML inference, and dashboard.

Communication between the cores uses **Bridge RPC** over an internal high-speed UART (460,800 baud).

```
┌─────────────────────────┐  Bridge RPC  ┌─────────────────────────┐
│  Arduino UNO Q (MCU)    │──────────►  │  Arduino UNO Q (MPU)    │
│  (STM32U585)            │  MessagePack │  (QRB2210 Linux)        │
│                         │              │                         │
│  LIS3DH  (I²C 400kHz)  │              │  Thread 1: Bridge ingest │
│  ├─ 3-axis accel 400Hz  │              │  ├─ arduino-router      │
│  DS18B20 (1-Wire async) │              │  └─ CircularBuffer      │
│  └─ board temp ±0.5°C   │              │                         │
│                         │              │  Thread 2: Inference 2Hz │
│  FIFO watermark ISR      │              │  ├─ ONNX model (or RMS)  │
│  └─ 25 samples/burst    │              │  └─ JSON → stdout        │
└─────────────────────────┘              │                         │
                                         │  FastAPI dashboard        │
                                         │  ├─ Operator view         │
                                         └─ Engineering telemetry   ┘
```

**Payload struct** (24 bytes, packed):
```c
struct SensorPayload {
    uint32_t timestamp_us;   // μs since boot
    uint32_t sequence_id;    // monotonic counter
    float    accel_x;        // m/s² (LIS3DH)
    float    accel_y;        // m/s² (LIS3DH)
    float    accel_z;        // m/s² (LIS3DH)
    float    board_temp;     // °C (DS18B20 waterproof probe, ±0.5 °C)
};
```

---

## Hardware

| Component | Role |
|---|---|
| Arduino UNO Q | Integrated MCU (STM32) + MPU (Linux) platform |
| Adafruit LIS3DH STEMMA QT | 3-axis accelerometer, I²C (0x18), 400 Hz ODR, FIFO watermark |
| DS18B20 waterproof probe | Board/motor-case temperature, 1-Wire, ±0.5 °C, 12-bit async |

**UNO Q Pin Mapping:**

| Function | UNO Q Pin | STM32 Pin | Notes |
|---|---|---|---|
| I2C SDA | SDA / Qwiic | PB11 | LIS3DH Data |
| I2C SCL | SCL / Qwiic | PB10 | LIS3DH Clock |
| LIS3DH INT | D2 | PB3 | FIFO Watermark Interrupt |
| DS18B20 | D4 | PA12 | 1-Wire Data (3.3V Pull-up) |

---

## Quickstart

### 1. MCU Firmware (STM32)

The firmware is located in `firmware/uno_q_main/`. It uses the Arduino Bridge library to stream data to the MPU.

1. Open `firmware/uno_q_main/uno_q_main.ino` in the Arduino IDE.
2. Select **Arduino UNO Q** as the board.
3. Upload to the board.

### 2. MPU Setup (Linux)

Connect to the UNO Q's MPU via SSH or the serial console.

```bash
git clone https://github.com/Tusker13-04/EdgeGuard
cd EdgeGuard
pip install -r requirements.txt
```

### 3. Capture training data

```bash
# Start ingestion in bridge mode
python main.py --mode bridge

# In a separate terminal, run capture
python capture_session.py --label normal --duration 30

# Attach a coin to the shaft, record imbalance
python capture_session.py --label imbalance --duration 30

# Output: data/raw/normal/*.csv and data/raw/imbalance/*.csv
# Upload these to Edge Impulse for training.
```

### 4. Live inference (rule-based fallback)

```bash
python main.py --mode bridge
# Emits JSON telemetry to stdout at 2Hz
```

### 5. Dashboard

```bash
uvicorn dashboard.server:app --host 0.0.0.0 --port 8080
# Open http://<uno-q-ip>:8080
```

### 6. Drop in ONNX model (after Edge Impulse training)

```bash
# Place the exported ONNX file here:
mkdir -p model
cp edgeguard.onnx model/edgeguard.onnx
# Restart main.py — it loads automatically. No code changes needed.
```

---

## Edge Impulse training

Training is done entirely in **Edge Impulse Studio** — no separate training script.

1. Upload CSVs from `data/raw/normal/` and `data/raw/imbalance/` to your EI project.
2. Create a **Spectral Analysis** DSP block (time + low-frequency spectral features).
3. Train a **1D CNN classifier** or **anomaly detection** block.
4. Export → **ONNX** or **C++ library**.
5. Copy `edgeguard.onnx` → `model/edgeguard.onnx` (step 6 above).

---

## Data format (Edge Impulse)

CSV files produced by `capture_session.py` are Edge Impulse-ready:

```
timestamp,accel_x,accel_y,accel_z,board_temp
0.0,0.123,-0.045,9.801,25.0
2.0,0.131,-0.042,9.798,25.0
4.0,...
```

- Timestamp in **milliseconds**, ~2.5 ms intervals (≈ 400 Hz effective rate)
- Edge Impulse infers sampling frequency from timestamp deltas
- Labels are encoded in the folder name (`normal/`, `imbalance/`)

---

## Repository structure

```
EdgeGuard/
├── firmware/                  # MCU Firmware
│   └── uno_q_main/
│       └── uno_q_main.ino     # STM32 source
├── src/                       # MPU Python pipeline
│   ├── bridge_receiver.py     # Arduino Bridge RPC client
│   ├── udp_receiver.py        # UDP client (legacy/testing)
│   ├── buffer.py              # FastCircularBuffer (numpy)
│   ├── capture.py             # Window slicing + CSV write
│   └── inference.py           # ONNX / RMS inference engine
├── dashboard/
│   ├── index.html             # Operator + Engineering UI
│   └── server.py              # FastAPI + WebSocket bridge
├── data/
│   └── raw/                   # Captured CSVs (gitignored contents)
│       ├── normal/
│       └── imbalance/
├── model/
│   └── edgeguard.onnx         # ← ADD THIS after EI training (gitignored)
├── tests/
│   └── test_pipeline.py
├── capture_session.py
├── main.py
├── requirements.txt
└── README.md
```

---

## Running tests

```bash
pip install pytest
pytest tests/ -v
```

---

## Limitations

- DS18B20 temperature is accurate to ±0.5 °C; used for motor-case thermal trend only.
- Effective sampling rate: 400 Hz (LIS3DH hardware ODR; FIFO watermark of 25 samples).
- ONNX model slot is wired but empty until Edge Impulse training is complete.
  The rule-based RMS fallback runs automatically until then.
- Bridge RPC ingestion depends on the `arduino-router` service being active on the MPU.
