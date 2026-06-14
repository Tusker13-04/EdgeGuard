import pytest
import numpy as np
import tempfile
import os
from unittest.mock import MagicMock, patch, PropertyMock
from src.capture import record_session, WINDOW_SIZE, N_FEATURES

@patch("time.sleep", return_value=None)
@patch("time.perf_counter")
def test_record_session_success(mock_perf_counter, mock_sleep):
    mock_perf_counter.side_effect = [0.0, 0.1, 0.2]
    buffer = MagicMock()
    type(buffer).total_written = PropertyMock(side_effect=[100, 500, 500, 500, 500])
    
    mock_snap = np.ones((1000, N_FEATURES), dtype=np.float32)
    buffer.get_snapshot.return_value = mock_snap
    
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = record_session(buffer, "normal", tmpdir, duration_seconds=1, countdown_seconds=0)
        assert len(paths) == 2

@patch("time.sleep", return_value=None)
@patch("time.perf_counter")
def test_record_session_timeout(mock_perf_counter, mock_sleep):
    # Simulate timeout
    mock_perf_counter.side_effect = [0.0, 5.0, 5.0, 5.0]
    
    buffer = MagicMock()
    type(buffer).total_written = PropertyMock(side_effect=[100, 300, 300, 300]) # 200 rows added
    
    mock_snap = np.ones((200, N_FEATURES), dtype=np.float32)
    buffer.get_snapshot.return_value = mock_snap
    
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = record_session(buffer, "normal", tmpdir, duration_seconds=1, countdown_seconds=0)
        assert len(paths) == 1

@patch("time.sleep", return_value=None)
@patch("time.perf_counter")
def test_record_session_not_enough_data(mock_perf_counter, mock_sleep):
    mock_perf_counter.side_effect = [0.0, 5.0, 5.0, 5.0]
    
    buffer = MagicMock()
    type(buffer).total_written = PropertyMock(side_effect=[100, 150, 150, 150]) # 50 rows
    
    mock_snap = np.ones((100, N_FEATURES), dtype=np.float32)
    buffer.get_snapshot.return_value = mock_snap
    
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ValueError, match="Captured only"):
            record_session(buffer, "normal", tmpdir, duration_seconds=1, countdown_seconds=0)

@patch("time.sleep", return_value=None)
@patch("time.perf_counter")
def test_record_session_countdown(mock_perf_counter, mock_sleep):
    mock_perf_counter.side_effect = [0.0, 0.1, 0.2]
    buffer = MagicMock()
    type(buffer).total_written = PropertyMock(side_effect=[100, 500, 500, 500])
    
    mock_snap = np.ones((1000, N_FEATURES), dtype=np.float32)
    buffer.get_snapshot.return_value = mock_snap
    
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = record_session(buffer, "normal", tmpdir, duration_seconds=1, countdown_seconds=2)
        assert len(paths) == 2
        assert mock_sleep.call_count >= 2

