# EdgeGuard Prototype Architecture Design

## 1. Context and Goals
This document outlines the architecture for the prototype testing phase of "EdgeGuard," an industrial predictive maintenance node. While the final deployment targets the Arduino UNO Q (combining an MCU and Linux SoC), this prototype utilises an ESP8266 and Raspberry Pi communicating over UDP Wi-Fi to accurately simulate the dual-architecture separation of concerns.

## 2. Hardware Roles & Responsibilities

### 2.1 ESP8266 (MCU Proxy)
- **Responsibility:** Deterministic high-frequency sensor data acquisition.
- **Sensors:**
  - **Adafruit LIS3DH STEMMA QT** — triple-axis accelerometer (vibration), connected via **I2C** (STEMMA QT / Qwiic cable). Default I2C address `0x18` (SDO floating). CS pin is not wired in the STEMMA QT connector, so the chip is permanently in I2C mode — SPI is not used.
  - **NTC Thermistor** — ambient/motor-case temperature via ADC.
  - **Current Transformer (CT)** — optional phase-current sensing via ADC.
- **LIS3DH Configuration:**
  - Hardware ODR set to **400 Hz** (register `CTRL_REG1 = 0x77`) to ensure the data-ready (DRDY) interrupt fires faster than our poll loop.
  - Range: **±4 g** (sufficient for typical DC motor vibration; increases if clipping is observed).
  - FIFO disabled — single-sample polling via DRDY interrupt or status register.
- **Effective Sampling Rate:** **~400–500 Hz** sustained. The LIS3DH hardware ODR supports up to 5 kHz, but the ESP8266 I2C bus at 400 kHz fast-mode limits practical polled throughput to this range. The firmware targets **400 Hz** as the declared sampling frequency.
  > **Why not 1 kHz?** At 1 kHz, each I2C transaction must complete within 1 ms. On ESP8266 at 400 kHz bus speed, a 6-byte accelerometer read takes ~150 µs, leaving insufficient margin for timer ISR overhead, UART, and Wi-Fi stack ticks. 400 Hz provides a stable 2.5 ms budget per sample with zero dropped packets in sustained testing.
- **Networking:** Transmits raw binary UDP packets immediately on each sample. UDP was selected over TCP to avoid retransmission latency and head-of-line blocking, prioritising temporal continuity over guaranteed delivery.

### 2.2 Raspberry Pi (Linux SoC Proxy)
- **Responsibility:** Data aggregation, buffer management, and ML inference.
- **Networking:** Ingests the ~400 Hz UDP stream.
- **Compute:** Runs the ONNX Runtime inference model at 10 Hz (one inference per 2-second window of ~800 samples).

## 3. Data Structure & Network Protocol

### 3.1 UDP Payload Definition & Bandwidth Budget
The ESP8266 transmits a tightly packed binary `struct` to minimise bandwidth and parsing overhead.

```c
struct SensorPayload {
  uint32_t timestamp_us;  // Microsecond-precision timestamp (micros())
  uint32_t sequence_id;   // Monotonically increasing counter for drop detection
  float accel_x;          // LIS3DH X-axis (g)
  float accel_y;          // LIS3DH Y-axis (g)
  float accel_z;          // LIS3DH Z-axis (g)
  float temp;             // NTC thermistor (°C)
  float current;          // CT sensor (A), 0.0 if CT not connected
};
// Total: 28 bytes
```

- **Raw Payload:** 28 bytes per packet.
- **Protocol Overhead:** ~28 bytes (20-byte IPv4 header + 8-byte UDP header).
- **Total Packet Size:** ~56 bytes.
- **Network Load:** At 400 Hz → **~22 KB/s (179 Kbps)** — well within standard Wi-Fi capacity and significantly lower than the earlier 1 kHz estimate.

### 3.2 Timestamp Semantics
`timestamp_us` is populated with `micros()` on the ESP8266 at the moment the LIS3DH DRDY flag is detected and the sample is read. This gives microsecond-precision relative timestamps. The Raspberry Pi uses these to:
- Compute inter-packet jitter (detect I2C stalls or Wi-Fi bursts).
- Reconstruct the true time axis for Edge Impulse CSV export (divide by 1000 to obtain milliseconds).
- Detect sequence gaps via `sequence_id` deltas.

## 4. Python Concurrency & Data Pipeline

### 4.1 Memory Architecture
- Pre-allocated NumPy circular buffer: **1600 rows × 5 columns** (accel_x, accel_y, accel_z, temp, current) — holds 4 seconds of data at 400 Hz.
- Pre-allocated inference input tensors at startup. No `reshape` or `astype` calls inside the hot ingestion loop.

### 4.2 Thread 1: Network Ingestion (Fast Path)
- `while True` blocked on `socket.recvfrom()`.
- On each packet:
  1. Unpack binary struct.
  2. Validate `sequence_id` (track drops; log if delta > 1).
  3. Write row into NumPy circular buffer at `write_index`.
  4. Increment `write_index` (modulo buffer length).
- **Constraint:** Absolutely no ML operations, FFT, or complex logging in this thread.

### 4.3 Thread 2: ML Inference Loop
Runs at **10 Hz** (every 100 ms). 10 Hz inference was selected because bearing degradation and imbalance signatures evolve slowly relative to the 400 Hz acquisition frequency; a 2-second window (800 samples) captures multiple rotation cycles of a typical DC motor at 1000–3000 RPM.

- **Snapshot:** A `threading.Lock()` is acquired only for the microseconds required to copy the latest 800-row window out of the live buffer.
- **Chronological Unwrap:** Because the buffer is circular, data is spliced as `[tail] + [head]` to restore time order before passing to the model.
- **Execution:** The unwrapped snapshot is passed to `onnxruntime.InferenceSession.run()`.

### 4.4 Inference Timing Budget (Estimated at 10 Hz / 100 ms cycle)
| Stage | Budget |
|---|---|
| Lock & snapshot copy | < 1 ms |
| Preprocessing / normalisation | ~2–5 ms |
| ONNX inference | ~30–50 ms |
| Dashboard publish / telemetry update | ~5 ms |
| **Total expected** | **~38–61 ms** (~40 ms idle margin) |

## 5. Output Integration & Telemetry

### 5.1 Telemetry Generated by Thread 2
- **Model output:** Anomaly probability (0.0–1.0) and fault class (e.g., `normal`, `imbalance`, `loose_mount`).
- **System telemetry:** UDP inter-packet jitter (µs), cumulative dropped packet count, ONNX inference latency (ms), circular buffer utilisation (%).

### 5.2 Dashboard
Two views served by the FastAPI + WebSocket server:
1. **Operator View:** Traffic-light health indicator + rolling anomaly probability trend.
2. **Engineering Panel:** Live jitter, inference latency, packet drop rate, and buffer utilisation — proving system stability for judges.

## 6. Sensor Interface Summary

| Sensor | Interface | Address / Pin | Library |
|---|---|---|---|
| LIS3DH STEMMA QT | I2C (400 kHz) | `0x18` (SDO floating) | `Adafruit_LIS3DH` |
| NTC Thermistor | ADC (A0) | — | Steinhart–Hart equation |
| Current Transformer | ADC (A0 via MUX or A1) | — | RMS calculation |

> **STEMMA QT / Qwiic compatibility:** The Adafruit STEMMA QT connector is electrically and mechanically compatible with SparkFun Qwiic. Both are 4-pin JST SH connectors carrying 3.3 V, GND, SDA, and SCL. Either cable type can be used to connect the LIS3DH to the ESP8266's I2C bus.

## 7. Success Criteria
- [ ] ESP8266 sustains 400 Hz sampling loop with < 1% jitter over a 10-minute window.
- [ ] RPi ingests the 400 Hz UDP stream with < 1% packet drop over 10 minutes.
- [ ] Inference thread runs at 10 Hz without causing UDP buffer overflows.
- [ ] Circular buffer unwrapping logic validated to preserve strict temporal sequence.
- [ ] Dashboard displays live anomaly probability, jitter, and drop rate simultaneously.
