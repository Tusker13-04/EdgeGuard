import numpy as np
from src.inference import run_inference_cycle
from src.buffer import FastCircularBuffer

def test_inference_cycle_calculates_telemetry():
    buf = FastCircularBuffer(capacity=1000, features=4)
    for _ in range(1000):
        buf.add_row(np.array([1.0, 1.0, 1.0, 25.0], dtype=np.float32))
    state = run_inference_cycle(buf)
    assert "anomaly_score" in state
    assert "inference_latency_ms" in state
    assert state["anomaly_score"] == 0.0
