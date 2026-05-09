import time
import numpy as np

def run_inference_cycle(buffer):
    start_time = time.perf_counter()
    snapshot = buffer.get_snapshot()
    time.sleep(0.035)
    avg_vibration = float(np.mean(snapshot[:, 0])) if len(snapshot) > 0 else 0.0
    anomaly_score = 1.0 if avg_vibration > 10.0 else 0.0
    latency_ms = (time.perf_counter() - start_time) * 1000
    return {
        "anomaly_score": anomaly_score,
        "inference_latency_ms": round(latency_ms, 2)
    }
