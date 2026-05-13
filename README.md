# EdgeGuard

> Industrial predictive maintenance node — ESP8266 + LIS3DH (I²C) + DS18B20 (1-Wire) + Raspberry Pi

EdgeGuard detects mechanical anomalies (imbalance, abnormal vibration) in rotating
machinery using a low-cost sensor node streaming to a Raspberry Pi for real-time
ML inference. Designed for the Arduino UNO Q target; this repository contains the
full working prototype on ESP8266 + Raspberry Pi.

---

## Architecture

```
┌─────────────────────────┐  UDP/Wi-Fi   ┌─────────────────────────┐
│  ESP8266 NodeMCU v2     │──────────►  │  Raspberry Pi            │
│                         │  24B/packet  │                          │
│  LIS3DH  (I²C 400kHz)  │              │  Thread 1: UDP ingest    │
│  ├─ 3-axis accel 400Hz  │              │  ├─ PacketParser          │
│  DS18B20 (1-Wire async) │              │  └─ CircularBuffer        │
│  └─ board temp ±0.5°C   │              │                          │
│                         │              │  Thread 2: Inference 2Hz │
│  FIFO watermark ISR      │              │  ├─ ONNX model (or RMS)  │
│  └─ 25 samples/burst    │              │  └─ JSON → stdout        │
└─────────────────────────┘              │                          │
                                         │  FastAPI dashboard        │
                                         │  ├─ Operator view         │
                                         └─ Engineering telemetry   ┘
```

**Payload struct** (24 bytes, little-endian):
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
| ESP8266 NodeMCU v2 | Sensor acquisition + UDP streaming |
| Adafruit LIS3DH STEMMA QT | 3-axis accelerometer, I²C (0x18), 400 Hz ODR, FIFO watermark |
| DS18B20 waterproof probe | Board/motor-case temperature, 1-Wire, ±0.5 °C, 12-bit async |
| Raspberry Pi (any) | ML inference, dashboard, data capture |

**LIS3DH → NodeMCU wiring (I²C via STEMMA QT cable):**

```
LIS3DH STEMMA QT   NodeMCU
VCC             →  3V3
GND             →  GND
SCL             →  D1  (GPIO5)
SDA             →  D2  (GPIO4)
INT1            →  D3  (GPIO0)   ← FIFO watermark interrupt
```

**DS18B20 → NodeMCU wiring (1-Wire):**

```
DS18B20    NodeMCU
VCC     →  3V3
GND     →  GND
DATA    →  D4  (GPIO2)   ← 4.7kΩ pull-up to 3.3V
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

- Timestamp in **milliseconds**, ~2.5 ms intervals (≈ 400 Hz effective rate on ESP8266 + LIS3DH via I²C)
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
│   ├── __init__.py
│   ├── udp_receiver.py
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

- DS18B20 temperature is accurate to ±0.5 °C; used for motor-case thermal trend only.
- Effective sampling rate on ESP8266 via I²C: 400 Hz (LIS3DH hardware ODR;
  FIFO watermark of 25 samples reduces interrupt overhead).
- ONNX model slot is wired but empty until Edge Impulse training is complete.
  The rule-based RMS fallback runs automatically until then.
- CWRU benchmark dataset (12 kHz) is used offline for model validation only;
  it must be downsampled to match your sensor's effective bandwidth before training.
- This prototype targets the Raspberry Pi. Final deployment target is Arduino
  UNO Q (combined MCU + Linux SoC).
