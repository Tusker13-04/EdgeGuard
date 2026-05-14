# src/buffer.py
# Thread-safe numpy circular buffer for the 400 Hz ingestion pipeline.
#
# Two threads access this:
#   Thread 1 (network/bridge) : calls add_row()      — lock held briefly
#   Thread 2 (inference)      : calls get_snapshot() — holds lock for full copy
#
# Design rationale for get_snapshot() holding the lock:
#   NumPy views are memory aliases, not snapshots.  Calling .copy() or
#   np.concatenate() outside the lock races with concurrent add_row() writes
#   (NumPy bulk memcpy releases the GIL internally).  At DEFAULT_CAPACITY=1600
#   rows × 4 float32 = 25.6 KB, a single np.concatenate inside the lock takes
#   ~30–80 µs — well within the 2.5 ms inter-row budget at 400 Hz.  The
#   prior optimisation of copying outside the lock was premature and incorrect.
#
# _total_written is a monotonically increasing counter that never saturates at
# capacity.  capture.py uses it as a watermark so post-warmup sessions always
# capture the correct number of rows even when the ring has already wrapped.

import numpy as np
import threading

from src.udp_receiver import N_FEATURES  # 4

# Default capacity: 4 seconds at 400 Hz = 1600 rows
DEFAULT_CAPACITY = 1600

# Column index constants — single source of truth for feature layout
ACCEL_COLS = slice(0, 3)   # indices 0,1,2 = accel_x, accel_y, accel_z
TEMP_COL   = 3             # index  3      = board_temp


class FastCircularBuffer:
    """
    Pre-allocated numpy ring buffer.
    Rows are written sequentially; when full, oldest rows are overwritten.
    get_snapshot() always returns rows in chronological order.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY, features: int = N_FEATURES):
        self.capacity      = capacity
        self.features      = features
        self._buf          = np.zeros((capacity, features), dtype=np.float32)
        self._write_idx    = 0
        self._is_full      = False
        self._total_written = 0   # FIX #3/#12: monotonic, never wraps
        self._lock         = threading.Lock()

    def add_row(self, row: np.ndarray) -> None:
        """Write one feature row.  Called from ingest thread only."""
        with self._lock:
            self._buf[self._write_idx] = row
            self._write_idx += 1
            if self._write_idx >= self.capacity:
                self._write_idx = 0
                self._is_full = True
            self._total_written += 1

    def get_snapshot(self) -> np.ndarray:
        """
        Returns a chronological copy of all rows currently in the buffer.

        FIX #1: the full copy and concatenation are now performed INSIDE the
        lock.  NumPy views captured inside the lock but copied outside are NOT
        safe — np.copy/concatenate release the GIL and concurrent add_row()
        writes can mutate the underlying memory mid-copy, producing torn
        float32 values that silently corrupt ONNX model inputs.

        At 25.6 KB (1600×4 float32) the lock is held for ~30–80 µs, which is
        acceptable against a 2.5 ms ingest cadence at 400 Hz.
        """
        with self._lock:
            wi   = self._write_idx
            full = self._is_full
            if not full:
                return self._buf[:wi].copy()
            # Unwrap ring in chronological order (oldest first) inside the lock
            return np.concatenate(
                (self._buf[wi:].copy(), self._buf[:wi].copy()), axis=0
            )

    @property
    def n_rows(self) -> int:
        with self._lock:
            return self.capacity if self._is_full else self._write_idx

    @property
    def total_written(self) -> int:
        """FIX #3/#12: monotonically increasing write count for capture watermarking."""
        with self._lock:
            return self._total_written

    def clear(self) -> None:
        """
        Reset the buffer to empty without re-allocating the underlying array.
        Also resets _total_written so watermark arithmetic stays consistent.
        """
        with self._lock:
            self._write_idx    = 0
            self._is_full      = False
            self._total_written = 0
            self._buf[:] = 0.0
