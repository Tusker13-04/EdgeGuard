# EdgeGuard Prototype Architecture Design

## 1. Context and Goals
This document outlines the architecture for the prototype testing phase of "EdgeGuard," an industrial predictive maintenance node. While the final deployment targets the Arduino UNO Q (combining an MCU and Linux SoC), this prototype utilizes an ESP8266 and Raspberry Pi communicating over UDP Wi-Fi to accurately simulate the dual-architecture separation of concerns.

## 2. Hardware Roles & Responsibilities

### 2.1 ESP8266 (MCU Proxy)
- **Responsibility:** Deterministic high-frequency sensor data acquisition.
- **Sensors:** MEMS accelerometer (vibration), NTC thermistor (temperature), Current Transformer.
- **Sampling Rate:** Hardware timers trigger exact 1kHz sampling.
- **Networking:** Transmits raw binary UDP packets immediately. UDP was selected over TCP to avoid retransmission latency and head-of-line blocking, prioritizing temporal continuity over guaranteed delivery.

### 2.2 Raspberry Pi (Linux SoC Proxy)
- **Responsibility:** Data aggregation, buffer management, and ML inference.
- **Networking:** Ingests the 1kHz UDP stream.
- **Compute:** Runs the ONNX Runtime quantized LSTM model at 10Hz.

## 3. Data Structure & Network Protocol

### 3.1 UDP Payload Definition & Bandwidth Budget
```c
struct SensorPayload {
  uint32_t timestamp_us;
  uint32_t sequence_id;
  float accel_x;
  float accel_y;
  float accel_z;
  float temp;
  float current;
};
```
- **Raw Payload:** 28 bytes per packet.
- **Protocol Overhead:** ~28 bytes (20 byte IPv4 header + 8 byte UDP header).
- **Total Packet Size:** ~56 bytes.
- **Network Load:** ~56 KB/s (448 Kbps) at 1kHz — well within standard Wi-Fi capacity.

## 4. Python Concurrency & Data Pipeline

### 4.1 Memory Architecture
- Pre-allocated Numpy circular buffer (2000 rows).
- Pre-allocated inference tensors at startup. No reshape/astype inside hot loops.

### 4.2 Thread 1: Network Ingestion
- `while True` blocked on `socket.recvfrom()`.
- Validates sequence_id, writes row into buffer, increments write_index.
- No ML or FFT operations in this thread.

### 4.3 Thread 2: ML Inference Loop
Runs at 10Hz. 10Hz inference was selected because bearing degradation signatures evolve slowly relative to the 1kHz acquisition frequency, allowing temporal aggregation without excessive compute load.

- Lock acquired only for microseconds for snapshot copy.
- Circular buffer chronologically unwrapped: `[tail] + [head]`.
- Snapshot fed to `onnxruntime.InferenceSession()`.

### 4.4 Inference Timing Budget
| Stage | Time |
|---|---|
| Lock & Snapshot Copy | < 1 ms |
| Preprocessing / Normalization | ~2-5 ms |
| ONNX Inference (Quantized LSTM) | ~30-50 ms |
| Dashboard Publish | ~5 ms |
| **Total** | **~40-61 ms** (healthy ~40ms margin) |

## 5. Output Integration & Telemetry

### 5.1 Telemetry
- Model Output: Anomaly probability (0.0–1.0) and fault classification.
- System Telemetry: UDP jitter, drop count, ONNX inference latency.

### 5.2 Dashboard
1. **Operator View:** Traffic-light indicator + anomaly trend.
2. **Engineering Panel:** Hidden view with live jitter, latency, packet drop rates.

## 6. Success Criteria
- [ ] ESP8266 maintains 1kHz loop without network blocking.
- [ ] RPi ingests 1kHz stream with <1% packet drop over 10 minutes.
- [ ] Inference thread runs at 10Hz without causing UDP buffer overflows.
- [ ] Circular buffer unwrapping strictly validates temporal sequence.
