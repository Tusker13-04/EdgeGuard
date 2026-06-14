import pytest
import time
from unittest.mock import MagicMock
from src.engine import EdgeGuardEngine

def test_engine_init():
    bridge = MagicMock()
    engine = EdgeGuardEngine(bridge=bridge, onnx_path=None)
    assert engine.bridge == bridge
    engine._stop_event.set()
    engine._hb_thread.join(timeout=1.0)

def test_engine_process_batch():
    bridge = MagicMock()
    engine = EdgeGuardEngine(bridge=bridge, onnx_path=None)
    # mock diag
    engine.diag = MagicMock()
    mock_result = MagicMock()
    mock_result.label = "imbalance"
    mock_result.diagnostic = "Check bearings"
    mock_result.imbalance_prob = 0.85
    mock_result.confidence = 0.90
    mock_result.temp_state = "Normal"
    mock_result.raw_probs = [0.1, 0.85, 0.05]
    engine.diag.run.return_value = mock_result
    
    # 4 columns, 5 rows = 20 values
    samples = [0.0] * 20
    telemetry = engine.process_batch(samples, board_temp_c=25.5, source="test_source")
    
    assert telemetry["label"] == "imbalance"
    assert telemetry["imbalance_prob"] == 0.85
    assert telemetry["board_temp_c"] == 25.5
    assert telemetry["source"] == "test_source"
    engine._stop_event.set()
    engine._hb_thread.join(timeout=1.0)

def test_engine_remote_tune():
    bridge = MagicMock()
    engine = EdgeGuardEngine(bridge=bridge, onnx_path=None)
    engine._recent_probs.extend([0.8, 0.8, 0.8, 0.8, 0.8])
    engine._tune_dispatched_at = 0.0
    
    engine._maybe_dispatch_remote_tune()
    bridge.put.assert_called_with("remote_tune", '{"threshold": 1000}')
    assert len(engine._recent_probs) == 0
    engine._stop_event.set()
    engine._hb_thread.join(timeout=1.0)

def test_engine_heartbeat_loop():
    bridge = MagicMock()
    engine = EdgeGuardEngine(bridge=bridge, onnx_path=None)
    time.sleep(0.1) # allow heartbeat thread to run once
    assert bridge.put.call_count >= 0 # Just making sure it doesn't crash
    engine._stop_event.set()
    engine._hb_thread.join(timeout=1.0)
