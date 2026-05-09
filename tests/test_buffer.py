import numpy as np
from src.buffer import FastCircularBuffer

def test_circular_buffer_snapshot():
    buf = FastCircularBuffer(capacity=5, features=5)
    for i in range(7):
        buf.add_row(np.array([i, i, i, i, i], dtype=np.float32))
    snap = buf.get_snapshot()
    assert snap.shape == (5, 5)
    assert snap[0][0] == 2.0
    assert snap[-1][0] == 6.0
