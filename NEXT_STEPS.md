# EdgeGuard Testing Guide (Arduino UNO Q)

This guide walks you through loading and testing the EdgeGuard system on your **Arduino UNO Q**. Since you are testing without the physical motor, you can simulate mechanical anomalies manually.

## 🧠 Dual-Brain Architecture Context

The EdgeGuard system leverages the true power of the Arduino UNO Q's dual-processor architecture:

1. **The Real-Time MCU (STMicroelectronics STM32U585):**
   - Runs the Zephyr RTOS C++ firmware (`uno_q_main.ino`).
   - Handles strict, deterministic 400Hz I2C ingestion from the accelerometer and 1-Wire temperature polling.
   - Executes the sub-millisecond "Reflex" logic to trigger physical hardware alerts instantly.

2. **The Linux MPU (Qualcomm QRB2210):**
   - Runs a full Debian OS and our Python AI Pipeline (`main.py`).
   - Executes the heavy ONNX neural network inference.
   - Communicates with AWS IoT Greengrass v2.
   - Dispatches dynamic threshold tuning back to the MCU based on historical anomaly probabilities.

The two brains communicate seamlessly using the internal RPC Unix domain socket (`arduino-router`).

---

## 🔌 Hardware Pinout & Wiring

Here is the exact pinout required for your current setup:

| Component | Arduino UNO Q Pin | Notes |
| :--- | :--- | :--- |
| **LIS3DH Accelerometer** | I2C (SDA / SCL) | Connect to the standard Arduino I2C headers or Qwiic port. The MCU firmware binds to `Wire1`. |
| **DS18B20 Temp Sensor** | Digital Pin `4` | **Crucial:** Requires a 4.7kΩ pull-up resistor bridging the Data pin and VCC. |
| **Reflex Alert LED** | Defined in `config.h` | Typically tied to the built-in LED or an external alert buzzer to test the zero-latency safety trip. |

> **⚠️ IMPORTANT VOLTAGE NOTE:** While the UNO Q has the classic Arduino form factor, many of its native high-speed I/O pins operate at **1.8V logic levels**. Always double-check your sensor breakout boards. If you are using the standard top headers, ensure they are running through the board's level shifters (usually 3.3V/5V compatible), otherwise you may damage the Qualcomm/STM32 logic pins!

---

## 🚀 Execution Commands

### Step 1: Flash the MCU (STM32)
Using the Arduino IDE (ensure you have the UNO Q board package installed):
1. Open `firmware/uno_q_main/uno_q_main.ino`.
2. Ensure you have the `ArduinoJson`, `OneWire`, `DallasTemperature`, and `ArduinoBridge` libraries installed.
3. Compile and upload to the board.
4. *The MCU will immediately begin sampling. If the MPU isn't running yet, it will enter "Safe-Local" fallback mode.*

### Step 2: Start the AI Pipeline (Qualcomm MPU)
SSH into the Arduino UNO Q's Linux environment, navigate to the repo, and launch the engine:

```bash
cd ~/EdgeGuard
source venv/bin/activate
python main.py --mode bridge
```

### Step 3: Test the Pipeline
1. **Baseline:** Leave the LIS3DH flat on your desk. You should see `normal` telemetry with an `imbalance_prob` near `0.00` printing to the terminal.
2. **Simulate Fault:** Vigorously shake or tap the accelerometer to simulate a broken motor bearing.
3. **Observe the Loop:**
   - The MPU (`main.py`) will detect the high variance, run the ONNX model, and output an `anomaly`.
   - If the anomaly persists for multiple cycles, the MPU will send a `remote_tune` JSON command back over the bridge.
   - The MCU will parse the new threshold using `ArduinoJson` and acknowledge the tune!
