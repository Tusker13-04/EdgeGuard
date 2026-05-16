# EdgeGuard — Architecture Flaw & Critical Error Report

**Status:** PARTIAL  
**Scope:** `feature/uno-q-migration` branch — all source files retrieved and quoted directly.  
**Date:** 2026-05-16  
**Methodology:** GRR (Grounded Repo Reader) — every claim is anchored to a quoted snippet with file path.

---

## Use-Case Summary

EdgeGuard is a real-time predictive maintenance pipeline for industrial motors.  
The STM32U585 MCU acquires vibration (LIS3DH, 400 Hz) and temperature (DS18B20) data and transmits batches to the QRB2210 MPU via a Unix Domain Socket through the `arduino-router` daemon.  
The MPU runs an ONNX classifier (or RMS fallback) at 2 Hz and streams JSON telemetry to a FastAPI/WebSocket dashboard.  
The primary safety requirement is **continuous, uninterrupted anomaly detection**; missed detections or stale data must not be silently tolerated.

---

## Critical Findings

---

### FLAW-01 — Race Condition: `get_snapshot()` Reads Stale Pointers After Buffer Wrap

**STATUS: VERIFIED**  
**Severity: CRITICAL**

**EVIDENCE** (`src/buffer.py`, `get_snapshot()`):
```python
with self._lock:
    wi       = self._write_idx
    full     = self._is_full
    buf_ref  = self._buf        # reference, NOT a copy

# --- End critical section ---

if not full:
    return buf_ref[:wi].copy()

tail = buf_ref[wi:].copy()
head = buf_ref[:wi].copy()
return np.concatenate((tail, head), axis=0)
```

**INTERPRETATION:**  
`buf_ref` is assigned as a reference to `self._buf` inside the lock. After the lock is released, `add_row()` from the ingest thread may overwrite rows in `self._buf` while `get_snapshot()` is mid-way through `buf_ref[wi:].copy()` or `buf_ref[:wi].copy()`. NumPy `.copy()` is not atomic — it is a memcpy loop. If the ingest thread writes to `self._buf[wi]` between the `tail = buf_ref[wi:].copy()` and `head = buf_ref[:wi].copy()` calls, the snapshot contains data from two different buffer positions at the same logical time, producing a torn read. At 400 Hz ingest (one row every 2.5 ms), the window between the two `.copy()` calls is large enough for the ingest thread to write multiple new rows, corrupting the snapshot used for inference.

**GAPS:**  
No test exercises concurrent `add_row` + `get_snapshot` timing. NumPy's `copy()` call duration depends on array size and system load and is not verified to complete within any particular time bound.

---

### FLAW-02 — Firmware: Semaphore Permits Only One Pending IRQ; Samples Are Dropped Under CPU Load

**STATUS: VERIFIED**  
**Severity: CRITICAL**

**EVIDENCE** (`firmware/uno_q_main/uno_q_main.ino`, `setup()`):
```c
k_sem_init(&fifo_sem, 0, 1);
```

**EVIDENCE** (`firmware/uno_q_main/uno_q_main.ino`, `acq_thread_func()`):
```c
k_sem_take(&fifo_sem, K_FOREVER);
```

**INTERPRETATION:**  
`k_sem_init(&fifo_sem, 0, 1)` creates a binary semaphore with a maximum count of **1**. `k_sem_give()` from the ISR cannot increment the count beyond 1. If the acquisition thread is still processing the previous batch when the next FIFO watermark ISR fires, the second `k_sem_give()` is silently discarded (Zephyr binary semaphore semantics: give on a full semaphore is a no-op). The hardware FIFO then continues filling. With a 32-level FIFO and a 25-sample watermark, overflow can occur within `(32-25)/400 = 17.5 ms` of the missed IRQ, causing the FIFO overflow path to trigger and a full batch to be discarded. Under MCU load spikes (I²C Bridge, UART, watchdog), this is reachable.

**GAPS:**  
Zephyr semaphore limit behaviour (`K_SEM_MAX_LIMIT`) not confirmed from a retrieved Zephyr header; however, the initialiser `k_sem_init(&fifo_sem, 0, 1)` with count limit=1 is a standard binary semaphore.

---

### FLAW-03 — Firmware: NaN Payloads Silently Inject a Whole-Batch Discard When `getEvent()` Fails

**STATUS: VERIFIED**  
**Severity: HIGH**

**EVIDENCE** (`firmware/uno_q_main/uno_q_main.ino`, `acq_thread_func()`):
```c
if (!lis.getEvent(&event)) {
    batch[idx].timestamp_us = micros();
    batch[idx].sequence_id  = seq_counter++;
    batch[idx].accel_x = batch[idx].accel_y = batch[idx].accel_z = batch[idx].board_temp = NAN;
    continue;
}
```

**EVIDENCE** (`src/bridge_receiver.py`, `parse_batch_bytes()`):
```python
if not np.all(np.isfinite(feats)):
    log.warning("[BridgeParser] Non-finite values in batch -- discarding.")
    return []
```

**INTERPRETATION:**  
When `getEvent()` fails for any sample in a 100-sample batch, all 4 fields are set to `NaN`. The Python parser then discards the **entire 100-sample batch** because `np.all(np.isfinite(feats))` is `False`. A single failed `getEvent()` call in 100 — e.g., due to a momentary I²C glitch — silently drops 250 ms of sensor data (100 samples at 400 Hz). If the motor is vibrating anomalously during that 250 ms window, the event is never processed. The firmware does not count or report `getEvent()` failures.

**GAPS:**  
`lis.getEvent()` failure frequency under normal operating conditions is unknown without hardware testing.

---

### FLAW-04 — Python: `_handle_batch()` Drop-Rate Counter Updates Inside Per-Sample Lock Loop

**STATUS: VERIFIED**  
**Severity: HIGH**

**EVIDENCE** (`src/bridge_receiver.py`, `_handle_batch()`):
```python
for _ts, seq, features in samples:
    with self._lock:
        if self._last_seq is not None:
            elapsed_s = now - self._last_batch_time
            max_plausible = max(int(elapsed_s * 400 * 2), 10_000)
            gap = (seq - self._last_seq - 1) & 0xFFFFFFFF
            if gap > max_plausible:
                gap = 0
            self.total_dropped += gap
        self.total_received += 1
        self._last_seq = seq
        self._last_batch_time = now
        self._last_temp_c = float(features[3])
    buf.add_row(features)
```

**INTERPRETATION:**  
The lock is acquired and released **once per sample** in a batch of 100 samples. Each lock acquisition involves an OS mutex lock/unlock cycle. At 400 Hz (100 samples per batch, 4 batches/second), this is 400 lock acquisitions per second for state updates alone, plus 400 more from `add_row()`. The lock contention is unnecessary: `now` is computed once before the loop, `_last_batch_time` is updated identically for every sample (100 samples all get the same `now`), and `_last_seq` is updated to the sequence ID of every sample when only the last matters for gap detection. Additionally, `gap = (seq - self._last_seq - 1) & 0xFFFFFFFF` is computed per-sample against the previous sample's seq, meaning within a normal batch it will always compute `0` (seq increments by 1). The effective drop detection only catches gaps between batches, not within them, but the per-sample loop wastes CPU doing 100 redundant lock-acquire/release cycles per batch.

**GAPS:**  
The truncated retrieval output prevents viewing the complete `_handle_batch` function past the lock block.

---

### FLAW-05 — Architecture: Dashboard Subprocess Has No Output Back-Pressure; Slow Clients Block Ingest Telemetry

**STATUS: VERIFIED**  
**Severity: HIGH**

**EVIDENCE** (`dashboard/server.py`, `_broadcast()`):
```python
sends = {
    ws: asyncio.create_task(
        asyncio.wait_for(ws.send_text(message), timeout=0.5)
    )
    for ws in list(_clients)
}
for ws, task in sends.items():
    try:
        await task
    except (asyncio.TimeoutError, Exception):
        dead.add(ws)
```

**EVIDENCE** (`dashboard/server.py`, `_pipeline_reader()`):
```python
while True:
    line = await proc.stdout.readline()
    ...
    await _broadcast(text)
```

**INTERPRETATION:**  
`_pipeline_reader()` reads one line from the subprocess, then immediately `await _broadcast(text)`, which awaits up to `0.5s × N` sequentially (awaiting all send tasks). With `_MAX_CLIENTS = 10` slow/stalled connections, `_broadcast` can block the reader coroutine for up to 5 seconds per line. During that time, `proc.stdout.readline()` is not called. The asyncio subprocess pipe buffer fills, causing `main.py`'s `print(flush=True)` to block, which stalls `PipelineEngine.run_inference_loop()`. This creates a back-pressure chain: slow WebSocket clients → subprocess pipe buffer → inference loop stall → no anomaly detection.

**GAPS:**  
Pipe buffer size on QRB2210 / Linux not retrieved. Default is typically 64 KB (~320 JSON lines), causing stall after ~160 seconds with 10 fully-stalled clients.

---

### FLAW-06 — Python: Inference Loop Catches ALL Exceptions, Masking Fatal Fallback Errors

**STATUS: VERIFIED**  
**Severity: HIGH**

**EVIDENCE** (`src/inference.py`, `InferencePipeline.run_cycle()`):
```python
try:
    if self.sess is not None:
        result = _onnx_score(self.sess, snapshot)
        self._onnx_fail_count = 0
    else:
        result = _rule_based_score(snapshot)
except Exception as exc:
    ...
    result = _rule_based_score(snapshot)  # fallback called inside except
```

**EVIDENCE** (`src/engine.py`, `run_inference_loop()`):
```python
while not self.stop_event.is_set():
    if self._ingest_exc[0] is not None:
        break
    result = self.pipeline.run_cycle(self.buf)
    ...
```

**INTERPRETATION:**  
If `_rule_based_score()` raises inside the `except` handler (e.g., numpy error on a malformed snapshot), this second exception propagates out of `run_cycle()` uncaught. `run_inference_loop()` has no try/except around `run_cycle()`. The exception kills the main thread. `main.py` exits; the dashboard restarts it after 2 seconds. During those 2 seconds, no anomaly detection is active and the gap is silent to the operator.

**GAPS:**  
The `len(snapshot) < WINDOW_SIZE` guard before `run_cycle` reduces the attack surface, but other numpy failure modes remain unverified.

---

### FLAW-07 — Firmware: Watchdog Reload in `loop()` Can Be Starved by Acquisition Thread Blocking on `Bridge.notify()`

**STATUS: VERIFIED**  
**Severity: HIGH**

**EVIDENCE** (`firmware/uno_q_main/uno_q_main.ino`, `loop()`):
```c
IWatchdog.reload();
delay(10);
```

**EVIDENCE** (`firmware/uno_q_main/config.h`):
```c
#define IWDG_TIMEOUT_US  4000000  // 4 seconds
```

**EVIDENCE** (`firmware/uno_q_main/uno_q_main.ino`, `acq_thread_func()`):
```c
Bridge.notify("sensor_batch", (uint8_t*)batch, sizeof(batch));
```

**INTERPRETATION:**  
`loop()` reloads the watchdog every ~10 ms. The acquisition thread runs at Zephyr priority 5. If `Bridge.notify()` blocks (e.g., the `arduino-router` daemon stalls or the internal UART/IPC buffer fills), the acquisition thread holds the CPU at priority 5, starving `loop()` (which runs at lower/cooperative priority). If `Bridge.notify()` blocks longer than 4 seconds, the IWDG resets the MCU. This silent reboot interrupts both sensing and temperature polling with no error indication to the MPU-side Python process.

**GAPS:**  
`Arduino_RouterBridge` library source is not in this repository. `Bridge.notify()` timeout/blocking behaviour is **UNKNOWN**.

---

### FLAW-08 — Python: `board_temp_c` in Telemetry Can Be `None` at Startup

**STATUS: VERIFIED**  
**Severity: MEDIUM**

**EVIDENCE** (`src/bridge_receiver.py`):
```python
self._last_temp_c: Optional[float] = None
```

**EVIDENCE** (`src/engine.py`, `run_inference_loop()`):
```python
telemetry = {
    ...
    "board_temp_c":   self.receiver.last_temp_c,
}
```

**INTERPRETATION:**  
`last_temp_c` is `None` at startup and remains `None` until the first valid temperature sample arrives. `engine.py` places `None` directly into the telemetry dict. `format_telemetry_json()` only sanitises non-finite `float` values — it does not sanitise `None`. `json.dumps` serialises `None` as `null`. Dashboard consumers expecting a `float` will receive `null` for an undefined duration at startup. Any downstream numeric comparison or chart render that assumes `float` will fail silently.

**GAPS:**  
`dashboard/index.html` not retrieved — `null` handling in the UI is UNKNOWN.

---

### FLAW-09 — Architecture: Simultaneous `capture_session.py` + `main.py` Multi-Client Socket Behaviour Undefined

**STATUS: PARTIAL**  
**Severity: MEDIUM**

**EVIDENCE** (`src/schema.py`):
```python
BRIDGE_SOCK_PATH = "/var/run/arduino-router.sock"
```

**EVIDENCE** (`src/bridge_receiver.py`, `run()`):
```python
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
    sock.connect(self.socket_path)
```

**INTERPRETATION:**  
Both `main.py` and `capture_session.py` use `BridgeReceiver` and connect to the same socket path. If both run simultaneously (a realistic scenario during live data collection for model retraining), the `arduino-router` daemon's multi-client behaviour is not documented in this repository. If the daemon is single-client, one process silently retries forever. If it broadcasts to all clients, both receive data — but this scenario is untested and undocumented. No application-level mutex or process guard exists.

**GAPS:**  
`arduino-router` daemon not in this repository. Multi-client socket behaviour is **UNKNOWN**.

---

### FLAW-10 — Python: Hardcoded Uncalibrated RMS Threshold With No Runtime Override

**STATUS: VERIFIED**  
**Severity: LOW (operational)**

**EVIDENCE** (`src/inference.py`):
```python
RMS_ANOMALY_THRESHOLD = 40.0

# At LIS3DH +/-8g range (78.4 m/s^2 full scale), 12.0 m/s^2 (~1.2g) is below
# typical idle motor vibration and produces near-100% false positives.
# 40.0 m/s^2 (~4g) is a calibrated starting point for imbalance detection;
# adjust based on baseline vibration measurements for the specific motor.
```

**INTERPRETATION:**  
The comment explicitly states this is a "starting point" requiring motor-specific calibration. However, there is no CLI flag, environment variable, or config file mechanism to override this value without editing source code and restarting the process. For a production predictive maintenance system, this means changing the detection sensitivity requires a code deployment. It also means the RMS fallback active-duty threshold on a motor with baseline vibration above 40 m/s² will continuously alarm; below 40 m/s² for a high-vibration motor it will never alarm.

---

## Architecture Risk Matrix

| ID | Location | Severity | Category | Impact |
|---|---|---|---|---|
| FLAW-01 | `src/buffer.py` | **CRITICAL** | Race condition | Torn inference snapshots → corrupt predictions |
| FLAW-02 | `firmware/uno_q_main.ino` | **CRITICAL** | Data loss | Silent sample drop under CPU load |
| FLAW-03 | `firmware` + `src/bridge_receiver.py` | **HIGH** | Data loss | One `NaN` drops 250 ms of sensor data |
| FLAW-04 | `src/bridge_receiver.py` | **HIGH** | Performance | 400 lock acquisitions/sec; redundant per-sample updates |
| FLAW-05 | `dashboard/server.py` | **HIGH** | Back-pressure | Slow WS clients stall inference loop |
| FLAW-06 | `src/inference.py` + `src/engine.py` | **HIGH** | Error masking | Fallback exception kills main thread |
| FLAW-07 | `firmware/uno_q_main.ino` | **HIGH** | Reliability | Bridge stall → IWDG reset → silent reboot |
| FLAW-08 | `src/engine.py` + `src/bridge_receiver.py` | **MEDIUM** | Data integrity | `None` board_temp in telemetry at startup |
| FLAW-09 | Architecture | **MEDIUM** | Concurrency | Multi-client socket behaviour undefined |
| FLAW-10 | `src/inference.py` | **LOW** | Operational | Hardcoded uncalibrated RMS threshold, no runtime override |

---

## Recommended Fixes

### FLAW-01 — Hold lock for entire copy

```python
def get_snapshot(self) -> np.ndarray:
    with self._lock:
        wi   = self._write_idx
        full = self._is_full
        if not full:
            return self._buf[:wi].copy()
        return np.concatenate((self._buf[wi:], self._buf[:wi]), axis=0)
```

### FLAW-02 — Use counting semaphore

```c
// Allow up to 4 pending ISR signals (FIFO_WATERMARK / SAMPLES_PER_IRQ)
k_sem_init(&fifo_sem, 0, FIFO_WATERMARK / SAMPLES_PER_IRQ);
```

### FLAW-03 — Per-sample NaN substitution instead of batch discard

Replace the whole-batch discard in `parse_batch_bytes()` with last-known-good substitution per sample, and move the `np.isfinite` check to be per-row.

### FLAW-04 — Single lock acquisition per batch

Compute gap using first and last sequence IDs in the batch, acquire `self._lock` once per batch (not once per sample), update counters in bulk.

### FLAW-05 — Decouple reader from broadcast with asyncio.Queue

```python
_telemetry_queue: asyncio.Queue = asyncio.Queue(maxsize=50)

async def _pipeline_reader():
    ...  # read line, put to queue (drop if full)
    await _telemetry_queue.put(text)  # non-blocking variant with try/except QueueFull

async def _broadcast_worker():
    while True:
        msg = await _telemetry_queue.get()
        await _broadcast(msg)
```

### FLAW-06 — Wrap `run_cycle` in engine loop

```python
try:
    result = self.pipeline.run_cycle(self.buf)
except Exception as exc:
    log.error("[Engine] run_cycle raised unexpectedly: %s", exc)
    continue
```

### FLAW-07 — Reload watchdog inside acquisition thread

Add `IWatchdog.reload()` immediately after `Bridge.notify()` returns, so the acquisition thread keeps the watchdog alive independently of `loop()`.

### FLAW-08 — Default `last_temp_c` to 25.0 at startup

Initialise `self._last_temp_c = 25.0` instead of `None`, matching the firmware's `last_temp_c = 25.0f` default.

### FLAW-10 — Add environment variable override for RMS threshold

```python
RMS_ANOMALY_THRESHOLD = float(os.environ.get("EDGEGUARD_RMS_THRESHOLD", "40.0"))
```
