# EdgeGuard — Industrial Predictive Maintenance Node

> **Prototype** — ESP8266 (sensor acquisition) + Raspberry Pi (ONNX inference + dashboard).  
> Simulates the dual-architecture of the target **Arduino UNO Q** (MCU + Linux SoC in one package).

---

## Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│                         EDGE NODE (Prototype)                             │
│                                                                           │
│  ┌─────────────────────────────┐       ┌──────────────────────────────┐  │
│  │      ESP8266 (MCU proxy)    │  UDP  │   Raspberry Pi (SoC proxy)   │  │
│  │                             │──────▶│                              │  │
│  │  MPU-6050  → I²C @ ~500 Hz  │  LAN  │  Thread 1 — UDP ingestion    │  │
│  │  NTC thermistor             │       │  Thread 2 — ONNX inference   │  │
│  │  (CT sensor optional)       │       │  FastAPI  — WS dashboard     │  │
│  │                             │       │                              │  │
│  │  Payload (28 bytes/packet): │       │  Circular buffer (NumPy)     │  │
│  │  timestamp_us | seq_id      │       │  Window: 500 samples         │  │
│  │  accel_xyz    | temp | curr │       │  Inference rate: 2 Hz        │  │
│  └─────────────────────────────┘       └──────────┬───────────────────┘  │
│                                                   │                       │
│                                     ┌─────────────▼──────────────────┐   │
│                                     │  ONNX Runtime (quantized)      │   │
│                                     │  Model: spectral features +    │   │
│                                     │  1D-CNN classifier / anomaly   │   │
│                                     │  Classes: normal | imbalance   │   │
│                                     └────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────────┘
                                            │
                               WebSocket (port 8765)
                                            │
                              ┌─────────────▼─────────────┐
                              │   dashboard/dashboard.html  │
                              │   Operator + Telemetry views│
                              └────────────────────────────┘
```

---

## Hardware

| Component | Role | Notes |
|-----------|------|-------|
| ESP8266 NodeMCU | MCU proxy — sensor acquisition | 1× hardware timer loop |
| MPU-6050 | 3-axis accelerometer (vibration) | I²C, ~250–500 Hz effective |
| NTC thermistor | Motor temperature | ADC pin, sampled each loop |
| Raspberry Pi 3B+ / 4 | SoC proxy — inference + dashboard | Python 3.11, ONNX Runtime |
| Small DC motor + fan | Demo load | Blu-Tack on shaft = imbalance |

---

## Repo Layout

```
EdgeGuard/
├── firmware/          # ESP8266 Arduino sketch (UDP transmitter)
├── pi/
│   ├── main.py        # UDP ingestion + ONNX inference (stdout JSON)
│   └── server.py      # FastAPI WebSocket bridge (reads main.py stdout)
├── training/
│   ├── collect.py     # Edge Impulse CSV capture helper
│   ├── cwru_prep.py   # Downsample CWRU 12 kHz → 500 Hz, label & export
│   └── export_onnx.py # PyTorch → ONNX export for Pi deployment
├── dashboard/
│   └── dashboard.html # Single-file live dashboard (no build step)
├── requirements.txt
└── README.md
```

---

## Sampling & CSV Format

ESP8266 targets a stable **~500 Hz** effective sampling rate (hardware limitations
of I²C + ESP8266 cap reliable throughput at 250–500 Hz; we measure actual
deltas in firmware and log them).

Edge Impulse CSV format (`training/collect.py` output):
```
timestamp,accX,accY,accZ,temp
0,0.012,-0.003,9.812,38.1
2,0.015,-0.001,9.809,38.1
4,...
```
- `timestamp` is in **milliseconds** (monotonically increasing, Δ = 1000/fs ms).
- Frequency is derived by Edge Impulse from timestamp deltas — no separate field needed.

---

## Training Pipeline

### Data sources
1. **Real DC motor captures** — `normal` (balanced shaft) and `imbalance` (Blu-Tack on shaft), recorded via `training/collect.py` at 500 Hz.
2. **CWRU bearing dataset** (auxiliary) — downsampled from 12 kHz → 500 Hz via `training/cwru_prep.py` using a low-pass anti-aliasing filter before decimation. Used to add validated fault signatures. **Not a 1:1 replication of CWRU lab conditions** — high-frequency content above ~250 Hz is discarded.

### Steps
```bash
# 1. Collect motor data
python training/collect.py --label normal    --duration 60 --out data/normal.csv
python training/collect.py --label imbalance --duration 60 --out data/imbalance.csv

# 2. Prep CWRU (optional but recommended)
python training/cwru_prep.py --src cwru_raw/ --out data/cwru_500hz/ --target-hz 500

# 3. Upload CSVs to Edge Impulse project, train spectral features + classifier
#    (or use training/export_onnx.py for a PyTorch 1D-CNN alternative)

# 4. Export ONNX model to pi/model.onnx
```

### Model
- **Input**: 500-sample window × 4 features (accX, accY, accZ, temp)
- **DSP block**: Time-domain + low-frequency spectral features (RMS, variance, spectral energy bands)
- **Classifier**: 1D-CNN or K-Means anomaly detector
- **Output**: `{normal, imbalance}` probabilities + anomaly score

---

## Deployment

### Install
```bash
pip install -r requirements.txt
```

### Run inference node
```bash
# Terminal 1 — inference (prints JSON lines to stdout)
python pi/main.py --model pi/model.onnx --host 0.0.0.0 --port 5005

# Terminal 2 — WebSocket bridge + dashboard server
python pi/server.py  # serves dashboard at http://<pi-ip>:8765
                     # WebSocket at ws://<pi-ip>:8765/ws
```

Open `dashboard/dashboard.html` in a browser (or navigate to `http://<pi-ip>:8765`).

### `main.py` stdout JSON schema
```json
{
  "inference": {
    "state":             "normal",
    "confidence":        0.94,
    "probabilities":     {"normal": 0.94, "imbalance": 0.06},
    "anomaly_score":     0.0031,
    "anomaly_threshold": 0.012,
    "infer_hz":          2.0,
    "latency_ms":        38.2,
    "preprocess_ms":     3.1,
    "snapshot_ms":       0.4,
    "cycle_ms":          41.7
  },
  "pipeline": {
    "packets_rx":        18403,
    "packets_dropped":   7,
    "drop_rate_pct":     0.038,
    "jitter_us":         120.4,
    "last_seq_gap":      0,
    "measured_hz":       498.3
  },
  "buffer": {
    "size":              2000,
    "write_index":       741
  },
  "sensor": {
    "temp_c":            41.2
  },
  "system": {
    "cpu_pct":           18.3,
    "ram_pct":           34.1,
    "uptime_s":          127,
    "ws_clients":        1
  }
}
```

---

## Limitations

- **Sensor bandwidth**: MPU-6050 at ~500 Hz captures fault signatures up to ~250 Hz. High-frequency bearing harmonics (kHz range) present in CWRU's 12 kHz data are not captured.
- **CWRU mismatch**: CWRU data is downsampled; the model learns low-frequency vibration signatures only. On-device demo uses a small DC motor, not a large industrial rig.
- **ESP8266 not an ML target**: ML inference runs entirely on the Pi. ESP8266 is a pure I/O device — not a first-class Edge Impulse deployment target.
- **Prototype only**: Final target is Arduino UNO Q (onboard MCU + Linux SoC). Current prototype mirrors that architecture split with discrete hardware.

---

## Acknowledgements

- [Case Western Reserve University Bearing Data Center](https://engineering.case.edu/bearingdatacenter) — publicly available bearing vibration dataset.
- [Edge Impulse](https://edgeimpulse.com) — time-series training and deployment tooling.
- [ONNX Runtime](https://onnxruntime.ai) — cross-platform inference engine.
