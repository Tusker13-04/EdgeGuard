# EdgeGuard CSV Capture Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a hands-free, countdown-driven CSV capture tool that records labeled sensor windows from the live UDP stream into Edge Impulse-compatible CSV files.

**Architecture:** `src/capture.py` orchestrates countdown and recording. It reuses `FastCircularBuffer` from `src/buffer.py` and `parse_payload` from `src/udp_receiver.py`. UDP ingestion runs in a background thread. The capture tool snapshots the buffer after a timed recording window and slices it into 500-row CSV files saved to `data/raw/<label>/`.

**Tech Stack:** Python 3.10+, Numpy, Pytest, `csv` (stdlib), `threading` (stdlib).

---

### File Structure Map
- `src/capture.py` — session countdown, buffer snapshot, CSV slicing and write.
- `tests/test_capture.py` — unit tests for window slicing and CSV format.
- `data/raw/normal/` — output directory for normal class CSVs (created at runtime).
- `data/raw/imbalance/` — output directory for imbalance class CSVs (created at runtime).
- `firmware/src/main.cpp` — modified to transmit 4 features only (drop current field).

---

### Task 1: Update ESP8266 Firmware to 4-Feature Payload

**Files:**
- Modify: `firmware/src/main.cpp`

- [ ] **Step 1: Update the SensorPayload struct to 4 features**

Replace the existing `SensorPayload` struct and loop body in `firmware/src/main.cpp` with:

```cpp
// firmware/src/main.cpp
#include <ESP8266WiFi.h>
#include <WiFiUdp.h>

const char* ssid = "YOUR_SSID";
const char* password = "YOUR_PASSWORD";
const char* hostIP = "192.168.1.100";
const int udpPort = 4444;

WiFiUDP udp;

struct __attribute__((packed)) SensorPayload {
  uint32_t timestamp_us;
  uint32_t sequence_id;
  float accel_x;
  float accel_y;
  float accel_z;
  float temp;       // NTC thermistor on A0
  // current reserved for UNO Q deployment
};

SensorPayload payload;
uint32_t seq_counter = 0;
unsigned long last_sample_time = 0;

void setup() {
  Serial.begin(115200);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) { delay(500); }
  udp.begin(udpPort);
}

void loop() {
  unsigned long current_time = micros();
  if (current_time - last_sample_time >= 1000) {
    last_sample_time = current_time;
    payload.timestamp_us = current_time;
    payload.sequence_id = seq_counter++;
    // Replace with real MPU-6050 I2C reads and NTC ADC read
    payload.accel_x = 1.0;
    payload.accel_y = 1.0;
    payload.accel_z = 9.8;
    payload.temp = 25.5;
    udp.beginPacket(hostIP, udpPort);
    udp.write((const uint8_t*)&payload, sizeof(SensorPayload));
    udp.endPacket();
  }
}
```

- [ ] **Step 2: Update `parse_payload` in `src/udp_receiver.py` to match 4-feature struct**

The struct is now `<LLffff` (2 longs + 4 floats = 24 bytes). Update:

```python
# src/udp_receiver.py
import struct
import numpy as np

PACKET_FORMAT = '<LLffff'  # timestamp_us, seq_id, accel_x, accel_y, accel_z, temp
PACKET_SIZE = struct.calcsize(PACKET_FORMAT)  # 24 bytes

def parse_payload(packet_bytes: bytes):
    unpacked = struct.unpack(PACKET_FORMAT, packet_bytes)
    timestamp = unpacked[0]
    seq_id = unpacked[1]
    features = np.array(unpacked[2:6], dtype=np.float32)  # accX, accY, accZ, temp
    return timestamp, seq_id, features
```

- [ ] **Step 3: Update existing test to match new format**

```python
# tests/test_udp_receiver.py
import struct
import numpy as np
from src.udp_receiver import parse_payload, PACKET_FORMAT

def test_parse_payload():
    packet = struct.pack(PACKET_FORMAT, 1000000, 42, 1.1, 2.2, 3.3, 25.5)
    timestamp, seq_id, features = parse_payload(packet)
    assert timestamp == 1000000
    assert seq_id == 42
    assert len(features) == 4
    assert np.isclose(features[0], 1.1, atol=1e-5)
    assert np.isclose(features[3], 25.5, atol=1e-5)
```

- [ ] **Step 4: Run tests to verify nothing broke**

Run: `pytest tests/ -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add firmware/src/main.cpp src/udp_receiver.py tests/test_udp_receiver.py
git commit -m "refactor: update payload to 4-feature struct (drop current, reserved for UNO Q)"
```

---

### Task 2: Window Slicing Logic

**Files:**
- Create: `src/capture.py`
- Create: `tests/test_capture.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capture.py
import numpy as np
from src.capture import slice_windows

def test_slice_windows_produces_correct_count():
    # 1500 rows, window_size=500 -> 3 windows, 0 remainder
    data = np.ones((1500, 4), dtype=np.float32)
    windows = slice_windows(data, window_size=500)
    assert len(windows) == 3
    assert windows[0].shape == (500, 4)

def test_slice_windows_drops_remainder():
    # 1600 rows, window_size=500 -> 3 windows, 100 rows dropped
    data = np.ones((1600, 4), dtype=np.float32)
    windows = slice_windows(data, window_size=500)
    assert len(windows) == 3

def test_slice_windows_preserves_values():
    data = np.arange(2000, dtype=np.float32).reshape(500, 4)
    windows = slice_windows(data, window_size=500)
    assert np.array_equal(windows[0], data)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_capture.py -v`
Expected: FAIL with "ImportError: cannot import name 'slice_windows'"

- [ ] **Step 3: Write minimal implementation**

```python
# src/capture.py
import os
import csv
import time
import numpy as np
from datetime import datetime, timezone

WINDOW_SIZE = 500
FEATURE_NAMES = ["accX", "accY", "accZ", "temp"]

def slice_windows(data: np.ndarray, window_size: int = WINDOW_SIZE):
    n_windows = len(data) // window_size
    return [data[i * window_size:(i + 1) * window_size] for i in range(n_windows)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_capture.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/capture.py tests/test_capture.py
git commit -m "feat: add window slicing logic for capture tool"
```

---

### Task 3: CSV Write Logic

**Files:**
- Modify: `src/capture.py`
- Modify: `tests/test_capture.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_capture.py
import os
import tempfile
from src.capture import save_window_as_csv

def test_csv_has_correct_header_and_row_count():
    window = np.ones((500, 4), dtype=np.float32)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = save_window_as_csv(window, label="normal", output_dir=tmpdir)
        with open(path) as f:
            lines = f.readlines()
        assert lines[0].strip() == "timestamp,accX,accY,accZ,temp"
        assert len(lines) == 501  # 1 header + 500 rows

def test_csv_timestamp_is_relative():
    window = np.ones((500, 4), dtype=np.float32)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = save_window_as_csv(window, label="normal", output_dir=tmpdir)
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert rows[0]["timestamp"] == "0"
        assert rows[499]["timestamp"] == "499"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_capture.py::test_csv_has_correct_header_and_row_count -v`
Expected: FAIL with "ImportError: cannot import name 'save_window_as_csv'"

- [ ] **Step 3: Add `save_window_as_csv` to `src/capture.py`**

```python
# Add to src/capture.py (after existing imports and constants)

def save_window_as_csv(window: np.ndarray, label: str, output_dir: str) -> str:
    label_dir = os.path.join(output_dir, label)
    os.makedirs(label_dir, exist_ok=True)
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    filepath = os.path.join(label_dir, f"{timestamp_str}.csv")
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp"] + FEATURE_NAMES)
        for i, row in enumerate(window):
            writer.writerow([i] + [round(float(v), 6) for v in row])
    return filepath
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_capture.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/capture.py tests/test_capture.py
git commit -m "feat: add CSV write with Edge Impulse-compatible format"
```

---

### Task 4: Countdown Session Orchestrator

**Files:**
- Modify: `src/capture.py`
- Create: `capture_session.py` (entry point, not in `src/`)

- [ ] **Step 1: Add `record_session` to `src/capture.py`**

```python
# Add to src/capture.py

def record_session(
    buffer,
    label: str,
    output_dir: str,
    duration_seconds: int = 30,
    countdown_seconds: int = 5,
    sample_rate: int = 1000
):
    print(f"\n>>> Recording: {label.upper()} — Starting in {countdown_seconds} seconds.")
    print("    Prepare motor state now.")
    for i in range(countdown_seconds, 0, -1):
        print(f"    {i}...", flush=True)
        time.sleep(1)
    print("    RECORDING...", flush=True)

    n_rows = duration_seconds * sample_rate
    captured_rows = []
    start = time.perf_counter()
    while len(captured_rows) < n_rows:
        snap = buffer.get_snapshot()
        if len(snap) > 0:
            captured_rows = list(snap)
        time.sleep(0.001)

    data = np.array(captured_rows[-n_rows:], dtype=np.float32)
    windows = slice_windows(data, window_size=WINDOW_SIZE)
    saved_paths = [save_window_as_csv(w, label=label, output_dir=output_dir) for w in windows]
    elapsed = time.perf_counter() - start
    print(f"    Done. {len(saved_paths)} windows saved to data/raw/{label}/  ({elapsed:.1f}s)")
    return saved_paths
```

- [ ] **Step 2: Create the entry point script**

```python
# capture_session.py  (run from repo root)
import socket
import threading
import argparse
from src.buffer import FastCircularBuffer
from src.udp_receiver import parse_payload
from src.capture import record_session

UDP_PORT = 4444
BUFFER_CAPACITY = 4000
FEATURES = 4
DATA_DIR = "data/raw"

def udp_ingest_thread(buf: FastCircularBuffer, stop_event: threading.Event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", UDP_PORT))
    sock.settimeout(1.0)
    while not stop_event.is_set():
        try:
            data, _ = sock.recvfrom(64)
            _, _, features = parse_payload(data)
            buf.add_row(features)
        except socket.timeout:
            continue
    sock.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EdgeGuard capture session")
    parser.add_argument("--label", required=True, choices=["normal", "imbalance"],
                        help="Fault class label for this session")
    parser.add_argument("--duration", type=int, default=30,
                        help="Recording duration in seconds (default: 30)")
    args = parser.parse_args()

    buf = FastCircularBuffer(capacity=BUFFER_CAPACITY, features=FEATURES)
    stop_event = threading.Event()
    t = threading.Thread(target=udp_ingest_thread, args=(buf, stop_event), daemon=True)
    t.start()

    record_session(buf, label=args.label, output_dir=DATA_DIR, duration_seconds=args.duration)
    stop_event.set()
```

- [ ] **Step 3: Run full test suite to verify nothing broke**

Run: `pytest tests/ -v`
Expected: 5 passed

- [ ] **Step 4: Commit**

```bash
git add src/capture.py capture_session.py
git commit -m "feat: add countdown session orchestrator and entry point"
```

---

## Usage After Implementation

```bash
# Record 30s of normal motor operation
python capture_session.py --label normal --duration 30

# Attach coin to shaft, then record 30s of imbalance
python capture_session.py --label imbalance --duration 30

# Output structure
data/raw/
  normal/
    20260510T001500123456.csv   # 500 rows each
    20260510T001500623456.csv
    ...  (60 files total)
  imbalance/
    20260510T002000123456.csv
    ...  (60 files total)
```

Upload the `data/raw/normal/` and `data/raw/imbalance/` folders directly to Edge Impulse via **Data Acquisition → Upload Existing Data**.
