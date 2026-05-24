# EdgeGuard — Full System Documentation

> **Project:** EdgeGuard — Hierarchical Distributed Inference for Industrial Condition Monitoring
> **Board:** Arduino UNO R4 WiFi (RA4M1, ARM Cortex-M4 @ 48 MHz, Zephyr RTOS)
> **Branch:** `feature/hierarchical-inference-mode`

---

## 1. The Problem We Solve

Industrial rotating machinery (motors, pumps, compressors) fails catastrophically when early vibration anomalies go undetected. Legacy solutions either:

- **React too slowly** — cloud-only pipelines introduce 100–500 ms latency before a safety stop, or
- **Lack context** — simple threshold alerts ("RMS > X") cannot distinguish *why* a machine is vibrating abnormally.

EdgeGuard solves both problems with a **two-tier distributed intelligence architecture**.

---

## 2. Architecture — Reflex → Cognition → Feedback

```
ARDUINO UNO R4 WIFI (RA4M1 · Cortex-M4 @ 48 MHz · Zephyr RTOS)

  LIS3DH --FIFO IRQ--> acq_thread
                           |
                   [EI REFLEX] (us latency, ~50 KB Flash, ~4 KB RAM)
                     /        \
               anomaly        normal
                 |               |
         Toggle D2          sensor_batch
         anomaly_trigger    (cheap pkt)
                 \              /
                  ArduinoBridge (USB/Serial)
                           |
                    MPU LAYER (Python · Qualcomm SDK · Linux)
                           |
              bridge_receiver --> EdgeGuardEngine
                                       |
                              DiagnosticEngine
                              (ONNX + multimodal fusion)
                                       |
                         Rich Telemetry (WebSocket)
                         { label, diagnostic, imbalance_prob,
                           board_temp_c, confidence, raw_probs }
                                       |
                         Remote-Tune Dispatcher
                         (prob > 0.7 for 5 batches
                          -> Bridge.put("remote_tune"))
                                       |
                      ArduinoBridge feedback
                                       |
              MCU: onCommand("remote_tune")
              -> hot-patch g_reflex_threshold at runtime
```

### Why this architecture wins

| Layer | Latency | Responsibility | Technology |
|---|---|---|---|
| **Reflex** (MCU) | < 1 ms | Instant safety stop, pin toggle | Edge Impulse TinyML + Zephyr RTOS |
| **Cognition** (MPU) | 20–80 ms | Contextual diagnosis, trend analysis | ONNX Runtime + multimodal fusion |
| **Feedback** (MPU→MCU) | async | Self-tuning threshold adaptation | ArduinoBridge `put` / `onCommand` |

> **Narrative for judges:** *"The MCU provides microsecond-latency safety reflexes — it stops the machine. The MPU provides millisecond-latency diagnostic cognition — it explains why and predicts what happens next. The feedback loop means the system gets smarter over time without redeployment."*

---

## 3. Bill of Materials (BOM)

| Component | Part Number / Version | Qty | Notes |
|---|---|---|---|
| Arduino UNO R4 WiFi | ABX00087 | 1 | RA4M1 SoC, Zephyr RTOS 3.4 |
| LIS3DH 3-axis accelerometer | SparkFun SEN-13963 | 1 | ±16 g, FIFO 32-level, I2C on Qwiic |
| DS18B20 temperature sensor | Maxim DS18B20+ | 1 | 1-Wire, −55 to +125 °C, ±0.5 °C |
| Qwiic cable (100 mm) | SparkFun PRT-14427 | 1 | I2C4 (Wire1) to LIS3DH |
| 4.7 kΩ pull-up resistor | CF14JT4K70 | 1 | DS18B20 1-Wire pull-up |
| LED + 330 Ω resistor | — | 1 | Reflex alert indicator on D2 |
| USB-C cable | — | 1 | ArduinoBridge (USB-CDC) |
| Host MPU | Any Linux SBC (tested: RPi 4B) | 1 | Python 3.10+, ONNX Runtime 1.17 |

### Software Versions

| Package | Version |
|---|---|
| Arduino IDE | 2.3.x |
| Zephyr RTOS | 3.4.0 (bundled with Arduino UNO R4 BSP) |
| ArduinoCore-renesas | 1.1.0 |
| Edge Impulse Arduino SDK | `edgeguard_vibration_inferencing` (export from EI Studio) |
| Python | 3.10+ |
| onnxruntime | 1.17.x |
| numpy | 1.26.x |
| Qualcomm AI Hub SDK | As per contest requirements |

---

## 4. Phase 1 — The Reflex (Firmware)

### What was built

- **`firmware/uno_q_main/config.h`** — Added `REFLEX_ALERT_PIN` (D2), `REFLEX_THRESHOLD` (2000 mg), `REMOTE_TUNE_CMD`.
- **`firmware/uno_q_main/uno_q_main.ino`** — Integrated Edge Impulse inferencing stub into `acq_thread_func`. When the EI model fires (`anomaly_confidence > 0.75`), the firmware:
  1. Immediately toggles `REFLEX_ALERT_PIN` HIGH (us-latency safety output).
  2. Calls `Bridge.notify("anomaly_trigger", ...)` to wake the MPU.
  
  When the model does not fire, it sends the cheaper `sensor_batch` packet.

### Replacing the stub with a real EI model

1. In [Edge Impulse Studio](https://studio.edgeimpulse.com), train a spectral + classifier model on your LIS3DH vibration data.
2. Export → **Arduino library** → download `.zip`.
3. In Arduino IDE: *Sketch → Include Library → Add .ZIP Library*.
4. Replace the stub `#include` in `uno_q_main.ino`:
   ```cpp
   #include <edgeguard_vibration_inferencing.h>
   ```
5. Verify Flash usage — the EON compiler typically produces 40–80 KB for a spectral model, well within 256 KB Flash budget.

### Memory budget

| Resource | Budget | EI model (spectral+classifier) | Margin |
|---|---|---|---|
| Flash | 256 KB | ~50–80 KB | ~176–206 KB |
| SRAM | 32 KB | ~3–5 KB | ~27–29 KB |

---

## 5. Phase 2 — The Cognition (MPU)

### What was built

- **`src/inference.py`** — Replaced bare ONNX wrapper with `DiagnosticEngine`. The engine:
  - Runs ONNX inference (or RMS fallback if model unavailable).
  - Classifies `board_temp_c` as `rising` (≥ 55 °C) or `normal`.
  - Looks up `(vibration_class, temp_state)` in a fusion matrix to produce a human-readable diagnostic string.
  - Returns a `DiagnosticResult` dataclass with `label`, `diagnostic`, `imbalance_prob`, `confidence`, `raw_probs`.

- **`src/engine.py`** — `EdgeGuardEngine.process_batch()` now emits rich telemetry including the `diagnostic` field. The remote-tune dispatcher is also wired here.

### Fusion matrix

| Vibration | Temperature | Diagnostic |
|---|---|---|
| imbalance | rising | Lubrication Failure — High Temp + High Vibration |
| imbalance | normal | Mechanical Imbalance — check shaft alignment |
| bearing | rising | Bearing Failure Imminent — schedule maintenance |
| bearing | normal | Bearing Wear — monitor closely |
| looseness | rising | Structural Looseness + Thermal Stress — urgent inspection |
| looseness | normal | Structural Looseness — inspect mountings |
| normal | rising | Thermal Anomaly — check cooling / lubrication system |
| normal | normal | Nominal Operation |

---

## 6. Phase 3 — The Feedback Loop (Remote Tuning)

### What was built

- **MPU side (`src/engine.py`):** `_maybe_dispatch_remote_tune()` monitors a rolling window of `imbalance_prob` values. If probability exceeds 0.70 for 5 consecutive batches, it calls `bridge.put("remote_tune", '{"threshold": 1500}')` — at most once every 60 seconds.

- **MCU side (`firmware/uno_q_main/uno_q_main.ino`):** `Bridge.onCommand("remote_tune", onRemoteTune)` parses the JSON payload and hot-patches `g_reflex_threshold` at runtime without requiring a reflash. An acknowledgment (`tune_ack`) is sent back to the MPU.

- **Dashboard (`dashboard/index.html`):** The fault overlay is now **gated on real telemetry** (`imbalance_prob > 0.80`), replacing the previous hardcoded demo. The `⚡ Remote-tuned` badge appears when the MPU has dispatched a tune command.

### Data flow (feedback loop)

```
MPU detects sustained high imbalance_prob
        |
bridge.put("remote_tune", {"threshold": 1500})
        |  ArduinoBridge
MCU: onRemoteTune() -> g_reflex_threshold = 1500
        |
MCU: Bridge.notify("tune_ack", "1500")
        |
MPU logs acknowledgment, resets rolling window
```

---

## 7. Key Design Decisions & Trade-offs

| Decision | Rationale |
|---|---|
| EI model on MCU, ONNX on MPU | EI's EON compiler targets Cortex-M4; full ONNX runtime is too large for 32 KB SRAM. |
| RMS fallback in DiagnosticEngine | Ensures the pipeline degrades gracefully if the ONNX model file is absent. |
| Temperature threshold at 55 °C | Conservative threshold for steel bearing housings; configurable via `EG_TEMP_RISING_C` env var. |
| Remote-tune throttled to 60 s | Prevents MCU spam under sustained fault conditions. |
| Dashboard overlay gated on prob > 0.8 | Avoids false-positive alerts; ensures judges see a *real* hardware event, not a demo. |
| `k_sem_init` max-count = 4 (FLAW-02 fix) | Prevents semaphore over-posting when FIFO watermark fires multiple times before thread wakes. |

---

## 8. Running the System

```bash
# 1. Flash firmware
cd firmware/uno_q_main
arduino-cli compile --fqbn arduino:renesas_uno:unor4wifi .
arduino-cli upload  --fqbn arduino:renesas_uno:unor4wifi --port /dev/ttyACM0 .

# 2. Install Python dependencies
pip install onnxruntime numpy

# 3. Start MPU pipeline
cd src
python main.py --model ../models/edgeguard_vibration.onnx

# 4. Open dashboard
open http://localhost:8080/dashboard/index.html
```

---

## 9. Future Work

- Replace EI stub with a real exported model trained on collected bearing datasets.
- Add `httpx` async HTTP client in `bridge_receiver.py` (replaces blocking `urllib.request`).
- Move `sys.stdin` hot-swap in `main.py` to a named pipe or local MQTT broker.
- Implement federated model updates: MCU sends labelled anomaly windows back to EI Studio for active learning.
- Add Kalman filter on `imbalance_prob` to smooth noisy inference before remote-tune dispatch.
