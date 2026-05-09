import numpy as np
import threading

class FastCircularBuffer:
    def __init__(self, capacity: int, features: int):
        self.capacity = capacity
        self.features = features
        self.buffer = np.zeros((capacity, features), dtype=np.float32)
        self.write_index = 0
        self.is_full = False
        self._lock = threading.Lock()

    def add_row(self, row: np.ndarray):
        self.buffer[self.write_index] = row
        self.write_index += 1
        if self.write_index >= self.capacity:
            self.write_index = 0
            self.is_full = True

    def get_snapshot(self) -> np.ndarray:
        with self._lock:
            if not self.is_full:
                return np.copy(self.buffer[:self.write_index])
            tail = self.buffer[self.write_index:]
            head = self.buffer[:self.write_index]
            return np.concatenate((tail, head), axis=0)
