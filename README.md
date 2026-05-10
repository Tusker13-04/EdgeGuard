# EdgeGuard

> Industrial predictive maintenance node — ESP8266 + LIS3DH + Raspberry Pi

EdgeGuard detects mechanical anomalies (imbalance, abnormal vibration) in rotating
machinery using a low-cost sensor node streaming to a Raspberry Pi for real-time
ML inference. Designed for the Arduino UNO Q target; this repository contains the
full working prototype on ESP8266 + Raspberry Pi.

---

## Architecture

```
┌─────────────────────────┐  UDP/Wi-Fi   ┌─────────────────────────┐
│  ESP8266 NodeMCU v2      │──────────►│  Raspberry Pi             │
│                         │  24B/packet  │                         │
│  LIS3DH (SPI 8MHz)      │             │  Thread 1: UDP ingest    │
│  ├─ 3-axis accel 400Hz  │             │  ├─ PacketParser          │
│  └─ embedded temp 1°C   │             │  └─ CircularBuffer        │
│                         │             │                         │
│  FIFO watermark ISR      │             │  Thread 2: Inference 2Hz │
│  └─ burst 25 samples     │             │  ├─ ONNX model (or RMS)   │
│     per UDP packet       │             │  └─ JSON → stdout          │
└─────────────────────────┘             │                         │
                                          │  FastAPI dashboard        │
                                          │  ├─ Operator view          │
                                          └─ Engineering telemetry   ┘
```

**Payload struct** (24 bytes, little-endian):
```c
struct SensorPayload {
    uint32_t timestamp_us;   // μs since boot
    uint32_t sequence_id;    // monotonic counter
    float    accel_x;        // m/s²
    float    accel_y;        // m/s²
    float    accel_z;        // m/s²
    float    board_temp;     // °C (LIS3DH ADC3, 1° resolution)
};
```

---

## Hardware

| Component | Role |
|---|---|
| ESP8266 NodeMCU v2 | Sensor acquisition + UDP streaming |
| Adafruit LIS3DH | 3-axis accelerometer, 400 Hz ODR, ±8g, FIFO burst |
| Raspberry Pi (any) | ML inference, dashboard, data capture |

**LIS3DH → NodeMCU wiring (SPI):**

```
LIS3DH    NodeMCU
VIN    →  3V3
GND    →  GND
SCK    →  D5  (GPIO14)
MISO   →  D6  (GPIO12)
MOSI   →  D7  (GPIO13)
CS     →  D8  (GPIO15)
INT1   →  D3  (GPIO0)
```

---

## Quickstart

### 1. Firmware (ESP8266)

```bash
cd firmware
# Edit src/main.cpp: set WIFI_SSID, WIFI_PASS, HOST_IP
pio run --target upload
pio device monitor
```

### 2. Raspberry Pi setup

```bash
git clone https://github.com/Tusker13-04/EdgeGuard
cd EdgeGuard
pip install -r requirements.txt
```

### 3. Capture training data

```bash
# Run motor normally for 30s
python capture_session.py --label normal --duration 30

# Attach a coin to the shaft, record imbalance
python capture_session.py --label imbalance --duration 30

# Output: data/raw/normal/*.csv and data/raw/imbalance/*.csv
# Upload these to Edge Impulse for training.
```

### 4. Live inference (rule-based fallback)

```bash
python main.py
# Emits JSON telemetry to stdout at 2Hz
```

### 5. Dashboard

```bash
uvicorn dashboard.server:app --host 0.0.0.0 --port 8080
# Open http://<pi-ip>:8080
```

### 6. Drop in ONNX model (after Edge Impulse training)

```bash
mkdir -p model
cp edgeguard.onnx model/edgeguard.onnx
# Restart main.py — ONNX loads automatically, no code changes needed.
```

---

## Data format (Edge Impulse)

CSV files produced by `capture_session.py` are Edge Impulse-ready:

```
timestamp,accel_x,accel_y,accel_z,board_temp
0.0,0.123,-0.045,9.801,25.0
2.5,0.131,-0.042,9.798,25.0
5.0,...
```

- Timestamp in **milliseconds**, 2.5 ms intervals (= 400 Hz)
- Edge Impulse infers sampling frequency from timestamp deltas
- Labels are encoded in the folder name (`normal/`, `imbalance/`)

---

## Running tests

```bash
pip install pytest
pytest tests/test_pipeline.py -v
# 17 tests, all modules covered
```

---

## Limitations

- LIS3DH embedded temperature has 1°C resolution; used for trend visualization
  only, not high-precision thermal monitoring.
- Effective sampling rate on ESP8266 via SPI: 400 Hz (hardware ODR). UDP packet
  rate: ~16 packets/sec (25 samples/packet via FIFO burst).
- ONNX model slot is wired but empty until Edge Impulse training is complete.
  The rule-based RMS fallback runs automatically until then.
- This prototype targets the Raspberry Pi. Final deployment target is Arduino
  UNO Q (combined MCU + Linux SoC).
