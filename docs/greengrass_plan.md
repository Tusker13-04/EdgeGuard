# EdgeGuard Context & AWS Greengrass v2 Integration Plan

This document persists the context regarding the Arduino UNO Q architecture, Edge Impulse training, and the proposed AWS IoT Greengrass v2 integration plan for the EdgeGuard repository.

## 1. Hardware Architecture: Arduino UNO Q (Hybrid MCU + MPU)
The Arduino UNO Q utilizes a heterogeneous "dual-brain" architecture:
*   **The MPU (Qualcomm QRB2210)**: An application processor running Debian Linux (quad-core Arm Cortex-A53). It handles the heavyweight processing, such as running the Python inference pipeline (`main.py`), ML (ONNX), and networking (WebSocket dashboard / AWS IPC).
*   **The MCU (STMicroelectronics STM32U585)**: A real-time microcontroller running Zephyr OS. It handles deterministic, low-latency sensor acquisition (LIS3DH at 400Hz) and batches telemetry.
*   **Communication**: The two cores communicate over a high-speed UART bridge via the `arduino-router` daemon, using MessagePack RPC on a Unix Domain Socket (`/var/run/arduino-router.sock`).

## 2. Model Training Workflow: Edge Impulse
Edge Impulse is used to train the vibration anomaly model:
*   **Data Capture**: The `capture_session.py` script records CSV files (timestamp, accX, accY, accZ, temp).
*   **Training**: Upload the CSVs to Edge Impulse. Use a "Spectral Analysis" block (for frequency features) followed by a 1D CNN Classifier.
*   **Deployment**: Export the trained model as an **ONNX** file.
*   **Integration**: Place the exported `edgeguard.onnx` into the `model/` directory. The Python pipeline (`DiagnosticEngine`) automatically loads it and expects an input shape of `(1, 200, 4)`.

## 3. AWS IoT Greengrass v2 Integration Plan
To integrate Greengrass v2, the QRB2210 MPU acts as the Greengrass Core device. The Python pipeline becomes a Greengrass Component.

### Core Device & Component Setup
*   **Nucleus**: Install the Greengrass Nucleus on the Debian Linux side of the UNO Q, configured with an IAM Token Exchange Service (TES) Role.
*   **Component Recipe (`recipe.yaml`)**:
    *   **Install Phase**: `python3 -m venv venv && venv/bin/pip install -r requirements.txt`
    *   **Run Phase**: `venv/bin/python3 -u main.py` (Unbuffered execution for native Greengrass log capture).
    *   **Configuration**: Migrate `EDGEGUARD_RMS_THRESHOLD` and `EDGEGUARD_ANOMALY_TRIGGER_THRESHOLD` to the recipe's `ComponentConfiguration` for cloud-based tuning.

### IPC & MQTT Telemetry
*   **Action**: Update `engine.py` to fully implement `GreengrassCoreIPCClientV2`.
*   **Flow**: Use `ipc_client.publish_to_iot_core(topic="edgeguard/telemetry", ...)` to send JSON inference results to AWS IoT Core over MQTT.
*   **Permissions**: Add `aws.greengrass.ipc.mqttproxy` permissions to the `recipe.yaml` so the component can publish out to the cloud.

### Over-The-Air (OTA) Model Updates
*   **Mechanism**: Use the AWS IoT Shadow service. The cloud updates a device shadow specifying the `desired` model version (e.g., `v1.2.onnx`).
*   **Subscription**: The Python pipeline subscribes to shadow delta events via IPC.
*   **Execution**: Upon a delta event, the pipeline downloads the new model artifact from S3 (using TES credentials) and hot-swaps the ONNX session without dropping the main process.

This setup securely bridges the deterministic edge data collection (MCU) with cloud-managed machine learning and telemetry (MPU via Greengrass).
