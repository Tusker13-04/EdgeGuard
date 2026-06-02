import numpy as np
from src.inference import DiagnosticEngine

def test_inference_cycle_calculates_telemetry():
    engine = DiagnosticEngine()
    # Mock 200 samples of 4-feature rows (accel_x, accel_y, accel_z, temp)
    samples = np.ones((200, 4), dtype=np.float32)
    result = engine.run(samples, board_temp_c=25.0)
    
    assert result.label is not None
    assert result.imbalance_prob <= 1.0
    assert result.temp_state == "normal"
    assert result.diagnostic is not None
    assert result.confidence <= 1.0
    assert result.label in ["normal", "imbalance", "bearing", "looseness"]
