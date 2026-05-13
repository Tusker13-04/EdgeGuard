# Design Spec: EdgeGuard Migration to Arduino UNO Q

**Date:** 2026-05-13  
**Status:** Approved  
**Target:** Arduino UNO Q (STM32U585 MCU + QRB2210 MPU)

---

## 1. Executive Summary
Migrate the EdgeGuard predictive maintenance prototype from ESP8266 + Raspberry Pi to the integrated Arduino UNO Q platform. This move consolidates the real-time sensor acquisition and high-level ML inference onto a single dual-core board, replacing Wi-Fi/UDP with internal Bridge RPC over UART.

## 2. Architecture

### 2.1 MCU (STM32U585)
- **Framework:** Arduino Framework (STM32 Core).
- **Sensors:**
    - **LIS3DH (Accelerometer):** I2C via Qwiic connector. 400Hz ODR. FIFO Watermark (25 samples) triggers EXTI interrupt on D2.
    - **DS18B20 (Temperature):** 1-Wire on D4 (3.3V with 4.7kΩ pull-up). Async conversion at 12-bit resolution.
- **Transport:** Bridge RPC (`Arduino_RouterBridge`).
    - **Method:** `Bridge.notify("sensor_data", payload, size)`.
    - **Strategy:** Low-latency batching at 460800 baud.

### 2.2 MPU (QRB2210)
- **OS:** Debian Linux.
- **Language:** Python 3.10+.
- **Ingestion:** `src/bridge_receiver.py` (New). Implements `Bridge.provide()` to receive MessagePack notifications from the MCU.
- **Pipeline:** 
    - Decoded samples pushed to `FastCircularBuffer` (numpy).
    - Inference loop runs at 2Hz using ONNX Runtime or RMS fallback.
- **Dashboard:** FastAPI server on port 8080 via Wi-Fi 5.

## 3. Data Contract
The 24-byte payload remains identical to ensure compatibility with existing capture and inference logic.

| Offset | Type | Field | Description |
|---|---|---|---|
| 0 | uint32 | timestamp_us | Microseconds since MCU boot |
| 4 | uint32 | sequence_id | Monotonic counter for drop detection |
| 8 | float | accel_x | X-axis acceleration (m/s²) |
| 12 | float | accel_y | Y-axis acceleration (m/s²) |
| 16 | float | accel_z | Z-axis acceleration (m/s²) |
| 20 | float | board_temp | Board/Motor temperature (°C) |

## 4. Hardware Mapping

| Function | UNO Q Pin | STM32 Pin | Notes |
|---|---|---|---|
| I2C SDA | SDA / Qwiic | PB11 | LIS3DH Data |
| I2C SCL | SCL / Qwiic | PB10 | LIS3DH Clock |
| LIS3DH INT | D2 | PB3 | FIFO Watermark Interrupt |
| DS18B20 | D4 | PA12 | 1-Wire Data (3.3V Pull-up) |

## 5. Success Criteria
- **Latency:** End-to-end ingestion latency (sensor to Python buffer) < 10ms.
- **Reliability:** 0% packet loss on the internal UART bridge.
- **Parity:** Dashboard shows identical metrics (accel, temp, probs) to the original prototype.
