# src/buffer.py
# Thread-safe numpy circular buffer for the 400Hz UDP ingestion pipeline.
#
# Two threads access this:
#   Thread 1 (network) : calls add_row() inside a lock
#   Thread 2 (inference): calls get_snapshot() inside a lock
#
# Lock held only for the duration of the write / the copy — not during inference.

import numpy as np
import threading

from src.udp_receiver import N_FEATURES  # 4

# Default capacity: 4 seconds at 400 Hz = 1600 rows
# Increase if you want longer inference windows
DEFAULT_CAPACITY = 1600


class FastCircularBuffer:
    """
    Pre-allocated numpy ring buffer.
    Rows are written sequentially; when full, oldest rows are overwritten.
    get_snapshot() always returns rows in chronological order.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY, features: int = N_FEATURES):
        self.capacity    = capacity
        self.features    = features
        self._buf        = np.zeros((capacity, features), dtype=np.float32)
        self._write_idx  = 0
        self._is_full    = False
        self._lock       = threading.Lock()

    def add_row(self, row: np.ndarray):
        """Write one feature row. Call from network thread only."""
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
        """
        with self._lock:
            if not self._is_full:
                return self._buf[:self._write_idx].copy()
            # Unwrap: tail (oldest) ++ head (newest)
            tail = self._buf[self._write_idx:]
            head = self._buf[:self._write_idx]
            return np.concatenate((tail, head), axis=0)

    @property
    def n_rows(self) -> int:
        with self._lock:
            return self.capacity if self._is_full else self._write_idx
