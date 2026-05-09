import os
import csv
import tempfile
import numpy as np
from src.capture import slice_windows, save_window_as_csv

def test_slice_windows_produces_correct_count():
    data = np.ones((1500, 4), dtype=np.float32)
    windows = slice_windows(data, window_size=500)
    assert len(windows) == 3
    assert windows[0].shape == (500, 4)

def test_slice_windows_drops_remainder():
    data = np.ones((1600, 4), dtype=np.float32)
    windows = slice_windows(data, window_size=500)
    assert len(windows) == 3

def test_slice_windows_preserves_values():
    data = np.arange(2000, dtype=np.float32).reshape(500, 4)
    windows = slice_windows(data, window_size=500)
    assert np.array_equal(windows[0], data)

def test_csv_has_correct_header_and_row_count():
    window = np.ones((500, 4), dtype=np.float32)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = save_window_as_csv(window, label="normal", output_dir=tmpdir)
        with open(path) as f:
            lines = f.readlines()
        assert lines[0].strip() == "timestamp,accX,accY,accZ,temp"
        assert len(lines) == 501

def test_csv_timestamp_is_relative():
    window = np.ones((500, 4), dtype=np.float32)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = save_window_as_csv(window, label="normal", output_dir=tmpdir)
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert rows[0]["timestamp"] == "0"
        assert rows[499]["timestamp"] == "499"
