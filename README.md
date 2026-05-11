# EdgeGuard

> Industrial predictive maintenance node — ESP8266 + MPU-6050 (I²C) + Raspberry Pi

EdgeGuard detects mechanical anomalies (imbalance, abnormal vibration) in rotating
machinery using a low-cost sensor node streaming to a Raspberry Pi for real-time
ML inference. Designed for the Arduino UNO Q target; this repository contains the
full working prototype on ESP8266 + Raspberry Pi.

---

## Architecture

```
┌─────────────────────────┐  UDP/Wi-Fi   ┌─────────────────────────┐
│  ESP8266 NodeMCU v2     │──────────►  │  Raspberry Pi            │
│                         │  28B/packet  │                          │
│  MPU-6050 (I²C 400kHz)  │              │  Thread 1: UDP ingest    │
│  ├─ 3-axis accel ~500Hz │              │  ├─ PacketParser          │
│  └─ on-chip temp        │              │  └─ CircularBuffer        │
│                         │              │                          │
│  Hardware-timer loop     │              │  Thread 2: Inference 2Hz │
│  └─ 1 sample/packet     │              │  ├─ ONNX model (or RMS)  │
│                         │              │  └─ JSON → stdout        │
└─────────────────────────┘              │                          │
                                         │  FastAPI dashboard        │
                                         │  ├─ Operator view         │
                                         └─ Engineering telemetry   ┘
```

**Payload struct** (28 bytes, little-endian):
```c
struct SensorPayload {
    uint32_t timestamp_us;   // μs since boot
    uint32_t sequence_id;    // monotonic counter
    float    accel_x;        // m/s²
    float    accel_y;        // m/s²
    float    accel_z;        // m/s²
    float    board_temp;     // °C (MPU-6050 on-chip sensor)
};
```

---

## Hardware

| Component | Role |
|---|---|
| ESP8266 NodeMCU v2 | Sensor acquisition + UDP streaming |
| MPU-6050 | 3-axis accelerometer + gyroscope, I²C, ~250–500 Hz effective ODR |
| Raspberry Pi (any) | ML inference, dashboard, data capture |

**MPU-6050 → NodeMCU wiring (I²C):**

```
MPU-6050   NodeMCU
VCC     →  3V3
GND     →  GND
SCL     →  D1  (GPIO5)
SDA     →  D2  (GPIO4)
AD0     →  GND  (I²C address 0x68)
INT     →  (optional) D3 (GPIO0)
```

> ⚠️ **Before running firmware:** you must create `firmware/src/secrets.h`  
> (see [Secrets setup](#secrets-setup) below).

---

## Quickstart

### 1. Secrets setup

`firmware/src/secrets.h` is **gitignored** (never commit credentials).  
Create it by copying the example template:

```bash
cp firmware/src/secrets.h.example firmware/src/secrets.h
# Then open firmware/src/secrets.h and fill in your values.
```

Contents to fill in:

```cpp
// firmware/src/secrets.h  ← NEVER COMMIT THIS FILE
#pragma once
#define WIFI_SSID   "your_wifi_ssid"
#define WIFI_PASS   "your_wifi_password"
#define HOST_IP     "192.168.x.x"   // Raspberry Pi IP on the same LAN
#define HOST_PORT   4210
```

### 2. Firmware (ESP8266)

```bash
cd firmware
pio run --target upload
pio device monitor
```

### 3. Raspberry Pi setup

```bash
git clone https://github.com/Tusker13-04/EdgeGuard
cd EdgeGuard
pip install -r requirements.txt
```

### 4. Capture training data

```bash
# Run motor normally for 30s
python capture_session.py --label normal --duration 30

# Attach a coin to the shaft, record imbalance
python capture_session.py --label imbalance --duration 30

# Output: data/raw/normal/*.csv and data/raw/imbalance/*.csv
# Upload these to Edge Impulse for training.
```

### 5. Live inference (rule-based fallback)

```bash
python main.py
# Emits JSON telemetry to stdout at 2Hz
```

### 6. Dashboard

```bash
uvicorn dashboard.server:app --host 0.0.0.0 --port 8080
# Open http://<pi-ip>:8080
```

### 7. Drop in ONNX model (after Edge Impulse training)

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
5. Copy `edgeguard.onnx` → `model/edgeguard.onnx` (step 7 above).

---

## Data format (Edge Impulse)

CSV files produced by `capture_session.py` are Edge Impulse-ready:

```
timestamp,accel_x,accel_y,accel_z,board_temp
0.0,0.123,-0.045,9.801,25.0
2.0,0.131,-0.042,9.798,25.0
4.0,...
```

- Timestamp in **milliseconds**, ~2 ms intervals (≈ 500 Hz effective rate on ESP8266 + MPU-6050 via I²C)
- Edge Impulse infers sampling frequency from timestamp deltas
- Labels are encoded in the folder name (`normal/`, `imbalance/`)

---

## Repository structure

```
EdgeGuard/
├── firmware/                  # PlatformIO project (ESP8266)
│   ├── src/
│   │   ├── main.cpp           # Sensor loop + UDP transmit
│   │   ├── secrets.h          # ← CREATE THIS (gitignored)
│   │   └── secrets.h.example  # Template — copy and fill in
│   └── platformio.ini
├── src/                       # Raspberry Pi Python pipeline
│   ├── udp_receiver.py
│   ├── circular_buffer.py
│   └── inference_engine.py
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
│   └── test_udp_receiver.py
├── capture_session.py
├── main.py
├── requirements.txt
└── README.md
```

> **Files you must add locally (not committed):**
> | Path | Action |
> |---|---|
> | `firmware/src/secrets.h` | Copy from `secrets.h.example`, fill in Wi-Fi + IP |
> | `model/edgeguard.onnx` | Export from Edge Impulse, place here |

---

## Running tests

```bash
pip install pytest
pytest tests/ -v
```

---

## Limitations

- MPU-6050 on-chip temperature has limited accuracy; used for trend only.
- Effective sampling rate on ESP8266 via I²C: ~250–500 Hz (hardware overhead
  limits the theoretical 1 kHz ODR; empirically measure and confirm before training).
- ONNX model slot is wired but empty until Edge Impulse training is complete.
  The rule-based RMS fallback runs automatically until then.
- CWRU benchmark dataset (12 kHz) is used offline for model validation only;
  it must be downsampled to match your sensor's effective bandwidth before training.
- This prototype targets the Raspberry Pi. Final deployment target is Arduino
  UNO Q (combined MCU + Linux SoC).
