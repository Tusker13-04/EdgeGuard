# EdgeGuard UNO Q Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate EdgeGuard from ESP8266 + Raspberry Pi to Arduino UNO Q, using Bridge RPC for inter-processor communication.

**Architecture:** The STM32U585 MCU handles real-time sensor data acquisition (400Hz) and batches data to the QRB2210 MPU via Bridge RPC over UART. The MPU runs the Python inference pipeline and dashboard.

**Tech Stack:** Arduino Framework (STM32), Arduino_RouterBridge library, Python 3.10+, numpy, ONNX Runtime.

---

### Task 1: Environment & Isolated Workspace

**Files:**
- Create: `.gitignore` (update)
- Action: Setup git worktree

- [ ] **Step 1: Create isolated worktree**

Run: `git worktree add .worktrees/feature-uno-q-migration -b feature/uno-q-migration`

- [ ] **Step 2: Install dependencies on MPU side**

Run: `pip install -r requirements.txt` (ensure on aarch64 environment)

- [ ] **Step 3: Verify baseline tests pass**

Run: `pytest tests/ -v`

---

### Task 2: MCU Firmware (Arduino UNO Q)

**Files:**
- Create: `firmware/uno_q_main/uno_q_main.ino`
- Modify: `firmware/platformio.ini` (optional, if using PIO for STM32)

- [ ] **Step 1: Create new Arduino sketch for UNO Q**

```cpp
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_LIS3DH.h>
#include <Adafruit_Sensor.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Arduino_RouterBridge.h>

#define LIS3DH_ADDR 0x18
#define LIS3DH_INT1_PIN 2
#define ONE_WIRE_PIN 4
#define FIFO_WATERMARK 25

struct __attribute__((packed)) SensorPayload {
    uint32_t timestamp_us;
    uint32_t sequence_id;
    float    accel_x;
    float    accel_y;
    float    accel_z;
    float    board_temp;
};

Adafruit_LIS3DH lis;
OneWire oneWire(ONE_WIRE_PIN);
DallasTemperature tempSensor(&oneWire);
SensorPayload batch[FIFO_WATERMARK];
uint32_t seq_counter = 0;
volatile bool fifo_ready = false;
float last_temp_c = 25.0f;

void onFifoWatermark() { fifo_ready = true; }

void setup() {
    Serial.begin(115200);
    Bridge.begin();
    Wire.begin();
    lis.begin(LIS3DH_ADDR);
    lis.setDataRate(LIS3DH_DATARATE_400_HZ);
    lis.setRange(LIS3DH_RANGE_8_G);
    // FIFO setup similar to original main.cpp
    pinMode(LIS3DH_INT1_PIN, INPUT);
    attachInterrupt(digitalPinToInterrupt(LIS3DH_INT1_PIN), onFifoWatermark, RISING);
    tempSensor.begin();
}

void loop() {
    // Temperature async read
    // ...
    if (fifo_ready) {
        fifo_ready = false;
        for (int i=0; i<FIFO_WATERMARK; i++) {
            lis.read();
            sensors_event_t event;
            lis.getEvent(&event);
            batch[i] = {micros(), seq_counter++, event.acceleration.x, event.acceleration.y, event.acceleration.z, last_temp_c};
        }
        Bridge.notify("sensor_batch", (uint8_t*)batch, sizeof(batch));
    }
}
```

- [ ] **Step 2: Commit initial firmware**

Run: `git add firmware/uno_q_main/uno_q_main.ino && git commit -m "feat(firmware): add initial UNO Q sketch with Bridge RPC"`

---

### Task 3: Python Bridge Receiver

**Files:**
- Create: `src/bridge_receiver.py`
- Test: `tests/test_bridge_receiver.py`

- [ ] **Step 1: Write failing test for Bridge Receiver**

```python
import pytest
from src.bridge_receiver import BridgeParser

def test_parse_batch():
    parser = BridgeParser()
    # Mock a 24-byte payload * 2 batch
    raw_data = b'\x00'*48 
    results = parser.parse_batch(raw_data)
    assert len(results) == 2
```

- [ ] **Step 2: Implement BridgeParser**

```python
import struct
import numpy as np

class BridgeParser:
    PACKET_FORMAT = '<LLffff'
    PACKET_SIZE = 24

    def parse_batch(self, data: bytes):
        samples = []
        for i in range(0, len(data), self.PACKET_SIZE):
            chunk = data[i:i+self.PACKET_SIZE]
            if len(chunk) < self.PACKET_SIZE: break
            ts, seq, ax, ay, az, temp = struct.unpack(self.PACKET_FORMAT, chunk)
            samples.append((ts, seq, np.array([ax, ay, az, temp], dtype=np.float32)))
        return samples
```

- [ ] **Step 3: Run tests and commit**

---

### Task 4: Main Integration

**Files:**
- Modify: `main.py`
- Modify: `src/udp_receiver.py` (abstract to support both)

- [ ] **Step 1: Refactor ingest thread to support Bridge**

- [ ] **Step 2: Update main.py to use BridgeReceiver when on UNO Q**

---

### Task 5: Verification & Docs

- [ ] **Step 1: Update README.md with UNO Q instructions**
- [ ] **Step 2: Run end-to-end verification (if hardware available)**
