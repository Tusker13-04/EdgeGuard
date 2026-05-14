# src/buffer.py
# Thread-safe numpy circular buffer for the 400 Hz ingestion pipeline.
#
# Two threads access this:
#   Thread 1 (network/bridge) : calls add_row()     — lock held briefly
#   Thread 2 (inference)      : calls get_snapshot() — expensive copy OUTSIDE lock
#
# Key design: get_snapshot() copies only scalar indices under the lock,
# then performs the (potentially slow) np.concatenate OUTSIDE the lock.
# This prevents the ingest thread from stalling while inference copies data.
#
# FIX (issue #2): added total_written monotonic counter.
# capture.py uses buffer.total_written as the recording-start watermark so that
# new_rows = total_written_end - total_written_start gives the exact sample
# count produced during a recording session, regardless of ring wrap-arounds.
# Without this attribute, capture.py raises AttributeError at runtime.

import numpy as np
import threading

from src.udp_receiver import N_FEATURES  # 4

# Default capacity: 4 seconds at 400 Hz = 1600 rows
DEFAULT_CAPACITY = 1600

# Column index constants — update here if firmware column order ever changes
ACCEL_COLS = slice(0, 3)   # indices 0,1,2 = accel_x, accel_y, accel_z
TEMP_COL   = 3             # index  3      = board_temp


class FastCircularBuffer:
    """
    Pre-allocated numpy ring buffer.
    Rows are written sequentially; when full, oldest rows are overwritten.
    get_snapshot() always returns rows in chronological order.

    Attributes
    ----------
    total_written : int
        Monotonically increasing count of all rows ever added via add_row().
        Never resets, even after the ring wraps.  Used by capture.py as a
        precise recording-start watermark that is immune to ring wrap-arounds.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY, features: int = N_FEATURES):
        self.capacity      = capacity
        self.features      = features
        self._buf          = np.zeros((capacity, features), dtype=np.float32)
        self._write_idx    = 0
        self._is_full      = False
        self.total_written = 0   # FIX #2: monotonic counter, never resets
        self._lock         = threading.Lock()

    def add_row(self, row: np.ndarray) -> None:
        """Write one feature row. Call from network/ingest thread only."""
        with self._lock:
            self._buf[self._write_idx] = row
            self._write_idx += 1
            self.total_written += 1   # FIX #2: increment before potential wrap
            if self._write_idx >= self.capacity:
                self._write_idx = 0
                self._is_full = True

    def get_snapshot(self) -> np.ndarray:
        """
        Returns a chronological copy of all rows currently in the buffer.
        If not yet full, returns only the rows written so far.

        The expensive np.concatenate / .copy() is performed OUTSIDE the lock
        so the ingest thread is never blocked by inference timing.
        """
        # --- Critical section: copy only O(1) scalars -----------------------
        with self._lock:
            wi       = self._write_idx
            full     = self._is_full
            buf_ref  = self._buf
        # --- End critical section --------------------------------------------

        if not full:
            return buf_ref[:wi].copy()

        # Unwrap ring: tail (oldest) ++ head (newest) — copies outside lock
        tail = buf_ref[wi:].copy()
        head = buf_ref[:wi].copy()
        return np.concatenate((tail, head), axis=0)

    @property
    def n_rows(self) -> int:
        with self._lock:
            return self.capacity if self._is_full else self._write_idx
