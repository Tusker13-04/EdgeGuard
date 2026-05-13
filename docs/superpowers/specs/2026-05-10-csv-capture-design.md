# EdgeGuard CSV Data Capture Tool — Design Spec

## 1. Context and Goals
This tool runs on the Raspberry Pi and records labeled sensor data from the live UDP stream into Edge Impulse-compatible CSV files. It is the data acquisition step that precedes Edge Impulse model training.

The prototype focuses on vibration-dominant anomaly signatures (LIS3DH accelerometer) while preserving modular support for current telemetry in the final Arduino UNO Q deployment.

## 2. Sensor Stack
| Sensor | Signal | Interface |
|---|---|---|
| LIS3DH STEMMA QT | accel_x, accel_y, accel_z | I²C (0x18) → ESP8266 |
| DS18B20 waterproof probe | temp | 1-Wire (GPIO2) → ESP8266 |
| Current (reserved) | — | ACS712 on UNO Q only (out of scope) |

## 3. Capture Parameters
| Parameter | Value |
|---|---|
| Sample rate | 400 Hz |
| Window size | 200 rows (500ms) |
| Features | accX, accY, accZ, temp |
| Classes | `normal`, `imbalance` |
| Default session duration | 30 seconds → 60 CSV files per session |
| Output path | `data/raw/<label>/<ISO_timestamp>.csv` |

## 4. Component Roles
- **`src/capture.py`** — orchestrates countdown, recording session, and CSV write. No UDP logic.
- **`src/udp_receiver.py`** — existing `parse_payload` reused as-is.
- **`src/buffer.py`** — existing `FastCircularBuffer` reused as-is.
- **`data/raw/<label>/`** — output directory, organized by class label for direct Edge Impulse upload.

## 5. Capture Session Flow
1. Script announces label and preparation prompt: `"Recording: NORMAL — Starting in 5 seconds. Prepare motor."`
2. Countdown: `5... 4... 3... 2... 1...` with 1-second sleeps.
3. UDP ingestion thread continues running throughout — buffer fills continuously.
4. Recording window opens: collects exactly `duration_seconds × 400` rows from the live buffer.
5. Capture array sliced into non-overlapping 200-row windows.
6. Each window saved as one CSV file.

## 6. Edge Impulse CSV Format
Each file contains exactly 201 lines (1 header + 200 data rows):
```
timestamp,accX,accY,accZ,temp
0,1.023,-0.512,9.812,25.4
1,1.031,-0.498,9.801,25.4
...
199,1.019,-0.501,9.808,25.3
```
- `timestamp` is a relative integer (0–199). Edge Impulse expects relative, not wall-clock timestamps.
- One file = one 500ms training sample (200 rows @ 400 Hz) = one class label.
- 30-second session → 60 CSV files per label.

## 7. Self-Review Checklist
- [ ] ESP8266 firmware updated to transmit accX, accY, accZ, temp (4 features, no current).
- [ ] Capture tool reuses existing buffer and UDP modules without duplication.
- [ ] Output CSVs match Edge Impulse ingestion format exactly.
- [ ] Session countdown gives operator time to prepare motor state hands-free.
