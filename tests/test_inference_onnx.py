import pytest
import numpy as np
from unittest.mock import patch, MagicMock
from src.inference import DiagnosticEngine

@patch("os.path.exists")
def test_diagnostic_engine_onnx_load_success(mock_exists):
    mock_exists.return_value = True
    
    with patch("builtins.__import__") as mock_import:
        mock_ort = MagicMock()
        mock_import.return_value = mock_ort
        
        mock_session = MagicMock()
        mock_ort.InferenceSession.return_value = mock_session
        
        # Test initialization
        engine = DiagnosticEngine(onnx_path="dummy.onnx")
        assert engine._session == mock_session
        
        # Test inference
        # Mock inputs and outputs
        mock_inp = MagicMock()
        mock_inp.name = "input"
        mock_session.get_inputs.return_value = [mock_inp]
        
        mock_out = MagicMock()
        mock_out.name = "output"
        mock_session.get_outputs.return_value = [mock_out]
        
        # Mock session.run output (logits)
        mock_session.run.return_value = [np.array([[-1.0, 2.0, 0.5, 0.0]])]
        
        samples = np.ones((200, 4), dtype=np.float32)
        result = engine.run(samples, board_temp_c=25.0)
        
        assert result.label == "imbalance"  # max logit is at index 1
        assert "Mechanical Imbalance" in result.diagnostic

@patch("os.path.exists")
def test_diagnostic_engine_onnx_load_fail(mock_exists):
    mock_exists.return_value = True
    with patch("builtins.__import__", side_effect=Exception("Failed to load onnxruntime")):
        engine = DiagnosticEngine(onnx_path="dummy.onnx")
        assert engine._session is None

def test_diagnostic_engine_rms_fallback_high():
    engine = DiagnosticEngine()
    # threshold is 150.0, so use high values
    samples = np.ones((200, 4), dtype=np.float32) * 200.0
    result = engine.run(samples, board_temp_c=25.0)
    assert result.label == "imbalance"
