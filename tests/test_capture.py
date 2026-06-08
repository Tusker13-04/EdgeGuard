import csv
import tempfile
import numpy as np
from src.capture import slice_windows, save_window_as_csv, WINDOW_SIZE
from src.schema import FEATURE_COLS, N_FEATURES

def test_slice_windows_produces_correct_count():
    data = np.ones((1000, N_FEATURES), dtype=np.float32)
    windows = slice_windows(data, window_size=200)
    assert len(windows) == 5
    assert windows[0].shape == (200, N_FEATURES)

def test_slice_windows_drops_remainder():
    data = np.ones((1050, N_FEATURES), dtype=np.float32)
    windows = slice_windows(data, window_size=200)
    assert len(windows) == 5

def test_slice_windows_preserves_values():
    data = np.arange(WINDOW_SIZE * N_FEATURES, dtype=np.float32).reshape(WINDOW_SIZE, N_FEATURES)
    windows = slice_windows(data, window_size=WINDOW_SIZE)
    assert np.array_equal(windows[0], data)

def test_csv_has_correct_header_and_row_count():
    window = np.ones((WINDOW_SIZE, N_FEATURES), dtype=np.float32)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = save_window_as_csv(window, label="normal", output_dir=tmpdir)
        with open(path) as f:
            lines = f.readlines()
        
        expected_header = "timestamp," + ",".join(FEATURE_COLS)
        assert lines[0].strip() == expected_header
        assert len(lines) == WINDOW_SIZE + 1

def test_csv_timestamp_is_relative():
    window = np.ones((WINDOW_SIZE, N_FEATURES), dtype=np.float32)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = save_window_as_csv(window, label="normal", output_dir=tmpdir)
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        
        assert rows[0]["timestamp"] == "0.0"
        # ROW_INTERVAL_MS is 2.5, so 199 * 2.5 = 497.5
        assert rows[WINDOW_SIZE - 1]["timestamp"] == "497.5"
