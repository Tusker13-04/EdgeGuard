# EdgeGuard Development Plan

This document outlines the current state and future architectural roadmap for the EdgeGuard system on the **Arduino UNO Q (STM32 MCU + Qualcomm MPU)** hybrid platform.

## Phase 1: Production Hardening (Completed)
We have successfully audited and hardened the existing Python pipeline running on the Qualcomm MPU and the C++ firmware on the STM32 MCU.

- **Security**: Mitigated XSS vulnerabilities in the dashboard, path traversal risks in the capture engine, and enforced TLS + Origin validation for network requests.
- **Reliability**: Replaced brittle assertions with runtime `ValueError` exceptions, hardened `msgpack` deserialization, and bounded thread creation to prevent resource exhaustion.
- **Concurrency**: Added thread-safe locks (`threading.Lock`) around critical state variables (e.g., `_recent_probs`, `_last_arrival`) to prevent race conditions during async FastAPI operations.
- **Memory Safety**: Promoted dynamic stack allocations (VLAs) in the Zephyr firmware to `static` allocation to prevent stack overflow on the MCU.
- **IPC Watchdog**: The telemetry heartbeat monitor is actively running. The MCU will force an automatic fallback to `safe-local` mode (high sensitivity) if the MPU Bridge stops communicating for >5000ms.

## Phase 2: MPU Thread Pooling & I/O Optimization (Next Steps)
Given the hybrid architecture, the Qualcomm MPU running Python is perfectly suited for asynchronous I/O management. Currently, some background tasks are handled using individual `threading.Thread` spawns. We will transition this to a managed Thread Pool.

1. **Implement `ThreadPoolExecutor`**
   - Refactor `src/bridge_receiver.py` and `src/engine.py` to use `concurrent.futures.ThreadPoolExecutor`.
   - **Target Tasks**: 
     - HTTP Webhook escalation (`_post_mode`).
     - Disk I/O operations (writing CSV captures).
     - Dashboard WebSocket broadcasts.
2. **Resource Throttling**
   - Bound the thread pool to a safe limit (e.g., `max_workers=4`) to ensure the Python interpreter's GIL (Global Interpreter Lock) doesn't block the critical inference loop.
3. **Graceful Shutdown**
   - Ensure the thread pool successfully drains and cleanly shuts down on SIGINT/SIGTERM without stranding processes.

## Phase 3: MCU ↔ MPU IPC Reliability
The IPC (Inter-Process Communication) bridge between the STM32 and Qualcomm needs to be heavily scrutinized for high-throughput reliability.

1. **Backpressure Handling**
   - If the MPU Thread Pool backs up, the MCU must gracefully drop or aggregate frames rather than crashing the Unix Domain Socket buffer.

## Phase 4: CI/CD & OTA Deployment
1. **Containerization**
   - Dockerize the Python pipeline so it can be deployed predictably to the Qualcomm MPU.
2. **Over-The-Air (OTA) Updates**
   - Set up an OTA pipeline for safely flashing `uno_q_main.ino` firmware payloads to the STM32 while keeping the MPU runtime stable.
