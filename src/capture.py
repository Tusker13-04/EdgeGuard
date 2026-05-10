# src/capture.py
# Capture sessions: slice the live circular buffer into labelled CSV windows
# for Edge Impulse ingestion.
#
# Edge Impulse CSV format requirements:
#   - Header: timestamp, <feature_cols...>
#   - timestamp column: milliseconds (monotonically increasing)
#   - Frequency inferred from timestamp deltas
#   - One file = one labelled sample
#
# At 400 Hz:
#   - 1 row  = 2.5 ms
#   - 200 rows = 500 ms window  (WINDOW_SIZE default)
#   - 30 s session = 12000 rows = 60 windows

import os
import csv
import time
import numpy as np
from datetime import datetime, timezone

from src.udp_receiver import FEATURE_COLS, N_FEATURES

# Sample rate must match LIS3DH ODR in firmware (LIS3DH_DATARATE_400_HZ)
SAMPLE_RATE_HZ = 400

# Window: 0.5 seconds of data at 400 Hz
WINDOW_SIZE    = 200  # rows

# Inter-row interval in milliseconds (for Edge Impulse timestamp column)
ROW_INTERVAL_MS = 1000.0 / SAMPLE_RATE_HZ  # 2.5 ms


def slice_windows(data: np.ndarray, window_size: int = WINDOW_SIZE):
    """Split a 2D array into non-overlapping windows of window_size rows."""
    n_windows = len(data) // window_size
    return [data[i * window_size:(i + 1) * window_size] for i in range(n_windows)]


def save_window_as_csv(window: np.ndarray, label: str, output_dir: str) -> str:
    """
    Save one window as a correctly-formatted Edge Impulse CSV.
    Timestamp column is in milliseconds, starting at 0 for each file.
    Returns the path of the saved file.
    """
    assert window.shape == (WINDOW_SIZE, N_FEATURES), (
        f"Window shape {window.shape} != ({WINDOW_SIZE}, {N_FEATURES})"
    )
    label_dir = os.path.join(output_dir, label)
    os.makedirs(label_dir, exist_ok=True)

    ts_str   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    filepath = os.path.join(label_dir, f"{ts_str}.csv")

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp"] + FEATURE_COLS)
        for i, row in enumerate(window):
            timestamp_ms = round(i * ROW_INTERVAL_MS, 3)
            writer.writerow([timestamp_ms] + [round(float(v), 6) for v in row])

    return filepath


def record_session(
    buffer,
    label: str,
    output_dir: str,
    duration_seconds: int = 30,
    countdown_seconds: int = 5,
) -> list:
    """
    Wait for countdown, then capture `duration_seconds` of live data from
    `buffer`, slice into windows, and save each as an Edge Impulse CSV.

    Returns list of saved file paths.
    """
    n_rows_needed = duration_seconds * SAMPLE_RATE_HZ

    print(f"\n[Capture] Label: {label.upper()}")
    print(f"[Capture] Target: {n_rows_needed} rows ({duration_seconds}s @ {SAMPLE_RATE_HZ}Hz)")
    print(f"[Capture] Starting in {countdown_seconds}s — set motor state now.")
    for i in range(countdown_seconds, 0, -1):
        print(f"          {i}...", flush=True)
        time.sleep(1)
    print("[Capture] RECORDING", flush=True)

    # Poll until we have enough fresh rows in the buffer
    # Sleep 50ms between polls to avoid busy-waiting
    deadline = time.perf_counter() + duration_seconds + 2.0  # +2s grace
    while True:
        if buffer.n_rows >= n_rows_needed:
            break
        if time.perf_counter() > deadline:
            print("[Capture] WARNING: buffer did not fill in time. Saving what we have.")
            break
        time.sleep(0.05)

    snap    = buffer.get_snapshot()
    data    = snap[-n_rows_needed:].astype(np.float32)  # take most recent rows
    windows = slice_windows(data)
    paths   = [save_window_as_csv(w, label=label, output_dir=output_dir) for w in windows]

    print(f"[Capture] Done. {len(paths)} windows → {output_dir}/{label}/")
    return paths
