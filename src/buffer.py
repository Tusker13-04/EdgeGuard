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
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY, features: int = N_FEATURES):
        self.capacity   = capacity
        self.features   = features
        self._buf       = np.zeros((capacity, features), dtype=np.float32)
        self._write_idx = 0
        self._is_full   = False
        self._lock      = threading.Lock()

    def add_row(self, row: np.ndarray) -> None:
        """Write one feature row. Call from network/ingest thread only."""
        with self._lock:
            self._buf[self._write_idx] = row
            self._write_idx += 1
            if self._write_idx >= self.capacity:
                self._write_idx = 0
                self._is_full = True

    def get_snapshot(self) -> np.ndarray:
        """
        Returns a chronological copy of all rows currently in the buffer.
        If not yet full, returns only the rows written so far.

        The expensive .copy() is performed OUTSIDE the lock so the ingest
        thread is never blocked by inference copy latency.

        FIX: previously buf_ref was a reference to self._buf captured inside
        the lock, but buf_ref[wi:].copy() ran OUTSIDE the lock.  If a future
        refactor ever replaces self._buf (e.g. resize), buf_ref would be
        stale and the copy would read from a detached array while the ingest
        thread wrote into the new one.  Instead we now capture numpy *views*
        (zero-copy slices) inside the lock and call .copy() on them outside.
        numpy slice objects hold a reference to the underlying data buffer,
        so they are safe to copy after releasing the lock as long as the
        array is not replaced — which this class never does.
        """
        with self._lock:
            wi   = self._write_idx
            full = self._is_full
            if not full:
                # Capture a view of the live portion only
                view = self._buf[:wi]      # O(1) — no copy inside lock
            else:
                # Capture both ring segments as views inside the lock
                # so wi cannot change between the two slice operations.
                tail_view = self._buf[wi:]  # oldest rows
                head_view = self._buf[:wi]  # newest rows

        # .copy() outside the lock — may be slow for large buffers
        if not full:
            return view.copy()

        tail = tail_view.copy()
        head = head_view.copy()
        return np.concatenate((tail, head), axis=0)

    @property
    def n_rows(self) -> int:
        with self._lock:
            return self.capacity if self._is_full else self._write_idx

    def clear(self) -> None:
        """
        Reset the buffer to empty without re-allocating the underlying array.
        Useful in test harnesses and capture sessions that reuse a buffer
        across multiple labelled recordings without restarting the process.
        """
        with self._lock:
            self._write_idx = 0
            self._is_full   = False
            # Zero the data so stale samples cannot leak into the next session
            self._buf[:] = 0.0
