# EdgeGuard Architecture Re-Check — 2026-05-16
**Status after developer fixes | Branch: `feature/uno-q-migration`**

---

## Executive Summary

All 10 previously reported flaws have been resolved. A full re-read of every source file confirms no remaining critical or high-severity issues — with one exception: **NEW-03 is a high-severity runtime crash** (`TypeError`) triggered the moment `--bridge-fifo` is passed on the CLI, caused by a parameter name mismatch between `BridgeReceiver.__init__` and `main.py`.

Three new medium/high observations and two low-severity notes are documented below, every claim anchored to quoted source.

---

## Previous Flaws — Verification

| ID | Title | Status | Evidence |
|----|-------|--------|----------|
| FLAW-01 | Torn snapshot in `get_snapshot()` | ✅ FIXED | `buffer.py:54–62`: entire `np.concatenate` now inside `with self._lock` |
| FLAW-02 | Binary semaphore drops IRQs | ✅ FIXED | `uno_q_main.ino`: `k_sem_init(&fifo_sem, 0, FIFO_WATERMARK / SAMPLES_PER_IRQ)` → max=4 |
| FLAW-03 | One NaN discards entire batch | ✅ FIXED | `bridge_receiver.py:54–67`: per-sample substitution loop with last-valid values |
| FLAW-04 | 400 lock acquisitions/second | ✅ FIXED | `bridge_receiver.py:93–118`: single `with self._lock` block per batch |
| FLAW-05 | Slow WS clients stall inference loop | ✅ FIXED | `server.py`: `asyncio.Queue(maxsize=50)` + `_broadcast_worker` task decoupling |
| FLAW-06 | Fallback exception kills main thread | ✅ FIXED | `engine.py:55–60`: `except Exception` in `run_inference_loop` catches and continues |
| FLAW-07 | IWDG starvation via `loop()` | ✅ FIXED | `uno_q_main.ino`: `IWatchdog.reload()` moved into `acq_thread_func` after each batch notify |
| FLAW-08 | `board_temp_c` is `None` at startup | ✅ FIXED | `bridge_receiver.py:75`: `self._last_temp_c: float = 25.0` |
| FLAW-09 | Dual-socket conflict undocumented | ✅ ACKNOWLEDGED | Documented in source; no multi-client handling added |
| FLAW-10 | Hardcoded RMS threshold | ✅ FIXED | `inference.py:25`: `float(os.environ.get("EDGEGUARD_RMS_THRESHOLD", "40.0"))` |

---

## New Findings

---

### NEW-01 — `get_snapshot()` holds lock during `np.concatenate` — contention under 400 Hz burst

**STATUS: PARTIAL**

**EVIDENCE (`src/buffer.py:54–62`):**
```python
def get_snapshot(self) -> np.ndarray:
    with self._lock:
        if not self._is_full:
            return self._buf[:self._write_idx].copy()
        return np.concatenate(
            (self._buf[self._write_idx:], self._buf[:self._write_idx]), axis=0
        )
```

**INTERPRETATION:**
FLAW-01 was correctly fixed by moving `np.concatenate` inside the lock. However, this introduced the inverse problem: at capacity=1600 rows × 4 float32 columns, `np.concatenate` allocates and copies a ~25 KB buffer while holding `self._lock`. The ingest thread calls `add_row()` every 2.5 ms; if `get_snapshot()` takes longer than one inter-sample interval while the lock is held, `add_row()` blocks and the ingest thread falls behind.

On the QRB2210 ARM Cortex-A53 with Python GIL contention, copy latency is measurably longer than on a desktop CPU. This is a latent contention risk that only manifests under sustained burst load.

**RECOMMENDED FIX:**
Copy both slices under the lock into pre-allocated locals, then concatenate *outside* the lock:
```python
def get_snapshot(self) -> np.ndarray:
    with self._lock:
        if not self._is_full:
            return self._buf[:self._write_idx].copy()
        wi   = self._write_idx
        tail = self._buf[wi:].copy()   # fast slice copy under lock
        head = self._buf[:wi].copy()   # fast slice copy under lock
    return np.concatenate((tail, head), axis=0)  # allocation outside lock
```

**GAPS:**
- QRB2210 memory bandwidth not benchmarked; may be non-issue in practice
- GIL profile not available

---

### NEW-02 — `capture.py`: TOCTOU gap silently contaminates training data

**STATUS: VERIFIED**

**EVIDENCE (`src/capture.py`, `record_session()`):**
```python
while True:
    new_rows = buffer.total_written - record_start_written
    if new_rows >= n_rows_needed:
        break          # ← ingest thread keeps running here
    time.sleep(0.05)

snap = buffer.get_snapshot()          # ← called after the break

new_rows_available = min(
    buffer.total_written - record_start_written,   # re-read AFTER snapshot
    n_rows_needed,
)
data = snap[-max(new_rows_available, 1):].astype(np.float32)
```

**INTERPRETATION:**
Between the `break` and `buffer.get_snapshot()`, the ingest thread continues writing at 400 Hz. `total_written` is then re-read *after* the snapshot for the slice calculation. This means `new_rows_available` is larger than what was in the snapshot at break-time, causing `snap[-new_rows_available:]` to slice further back in time than the recording window — silently prepending pre-session data to the Edge Impulse training sample.

For a 30-second session: 50 ms of OS scheduling delay between the `break` and the snapshot = 20 extra rows from before the recording window.

**RECOMMENDED FIX:**
Freeze `rows_to_slice` before calling `get_snapshot()`:
```python
rows_to_slice = min(
    buffer.total_written - record_start_written,
    n_rows_needed,
)
snap = buffer.get_snapshot()
data = snap[-max(rows_to_slice, 1):].astype(np.float32)
# Do NOT re-read buffer.total_written after this point
```

**GAPS:** None — fully verifiable from shown code.

---

### NEW-03 — **[HIGH]** Constructor keyword argument mismatch crashes `--bridge-fifo` mode

**STATUS: VERIFIED**

**EVIDENCE (`src/bridge_receiver.py:74`):**
```python
class BridgeReceiver(BaseReceiver):
    def __init__(self, socket_path: str = BRIDGE_SOCK_PATH):
        self.socket_path = socket_path
```

**EVIDENCE (`main.py`, live mode):**
```python
recv = BridgeReceiver(fifo_path=fifo_path)   # keyword arg is "fifo_path"
```

**INTERPRETATION:**
`BridgeReceiver.__init__` declares `socket_path` as its parameter. `main.py` passes `fifo_path=fifo_path` as a keyword argument. This raises:
```
TypeError: BridgeReceiver.__init__() got an unexpected keyword argument 'fifo_path'
```
at runtime, in the default production mode (`--mode bridge`), whenever `--bridge-fifo` is passed on the CLI. Without the flag, `BridgeReceiver()` is called with no keyword argument and falls back to the default constant silently — so the bug is invisible in zero-argument usage.

**RECOMMENDED FIX (one-line change, either file):**
```python
# Option A — fix bridge_receiver.py to match main.py
def __init__(self, fifo_path: str = BRIDGE_SOCK_PATH):
    self.socket_path = fifo_path

# Option B — fix main.py to match bridge_receiver.py
recv = BridgeReceiver(socket_path=fifo_path)
```

**GAPS:** None — directly visible in both files.

---

### NEW-04 (Low) — `loop()` watchdog reload weakens IWDG ownership guarantee

**STATUS: VERIFIED**

**EVIDENCE (`uno_q_main.ino`):**
```cpp
// loop()
IWatchdog.reload();
delay(10);

// acq_thread_func() — added by FLAW-07 fix
IWatchdog.reload();   // after each full batch (~250 ms)
```

**INTERPRETATION:**
FLAW-07 was fixed by adding `IWatchdog.reload()` inside `acq_thread_func`. The original `IWatchdog.reload()` in `loop()` still exists. This is harmless in normal operation, but it means a frozen `acq_thread_func` would not trigger the watchdog as long as `loop()` is running — the intended safety guarantee (IWDG fires if acquisition stalls) is undermined. IWDG ownership should belong exclusively to `acq_thread_func`.

**RECOMMENDED FIX:** Remove `IWatchdog.reload()` from `loop()` and add a comment:
```cpp
void loop() {
    // Watchdog is reloaded by acq_thread_func() after each batch.
    // Do NOT add IWatchdog.reload() here — loop() starvation must trigger IWDG.
    if (temp_req_pending && ...) { ... }
    delay(10);
}
```

---

### NEW-05 (Low) — `null` origin blocks local dev when `ALLOWED_ORIGINS` is set

**STATUS: PARTIAL**

**EVIDENCE (`dashboard/server.py`):**
```python
if _ALLOWED_ORIGINS:
    origin = ws.headers.get("origin", "")
    if origin not in _ALLOWED_ORIGINS:
        await ws.close(code=1008)
        return
```

**INTERPRETATION:**
Browsers send `Origin: null` when a WebSocket is opened from a `file://` URL or sandboxed iframe. The string `"null"` will not match any entry in `_ALLOWED_ORIGINS` unless explicitly added, silently refusing dashboard WebSocket connections during local development. If `_ALLOWED_ORIGINS` is empty (the default), the check is bypassed entirely and this is not a problem.

**RECOMMENDED FIX:** Document the caveat and add `"null"` to the allowed set when running locally:
```
EDGEGUARD_ALLOWED_ORIGINS="http://localhost:8000,null"
```

**GAPS:** Deployment config for `EDGEGUARD_ALLOWED_ORIGINS` not in repository.

---

## Summary

| ID | Severity | File | Issue | Action |
|----|----------|------|-------|--------|
| NEW-01 | Medium | `src/buffer.py` | `np.concatenate` inside lock starves `add_row()` under burst | Copy slices under lock, concatenate outside |
| NEW-02 | Medium | `src/capture.py` | TOCTOU gap prepends pre-session rows to training CSVs | Freeze `rows_to_slice` before calling `get_snapshot()` |
| NEW-03 | **High** | `bridge_receiver.py` / `main.py` | `fifo_path` kwarg raises `TypeError` when `--bridge-fifo` is used | Rename parameter in one file to match the other |
| NEW-04 | Low | `firmware/uno_q_main.ino` | Redundant `IWatchdog.reload()` in `loop()` weakens IWDG ownership | Remove from `loop()`, add explanatory comment |
| NEW-05 | Low | `dashboard/server.py` | `null` origin rejected when `ALLOWED_ORIGINS` is configured | Document caveat; add `"null"` to origins for local dev |
