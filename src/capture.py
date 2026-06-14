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
import re
import logging
import numpy as np
from datetime import datetime, timezone

from src.schema import FEATURE_COLS, N_FEATURES, SAMPLE_RATE_HZ, WINDOW_SIZE, ROW_INTERVAL_MS

def slice_windows(data: np.ndarray, window_size: int = WINDOW_SIZE):
    """Split a 2D array into non-overlapping windows of window_size rows."""
    n_windows = len(data) // window_size
    return [data[i * window_size:(i + 1) * window_size] for i in range(n_windows)]


def save_window_as_csv(window: np.ndarray, label: str, output_dir: str,
                       window_index: int = 0) -> str:
    """
    Save one window as a correctly-formatted Edge Impulse CSV.
    Timestamp column is in milliseconds, starting at 0 for each file.
    Returns the path of the saved file.

    window_index is appended to the filename to prevent timestamp collisions
    when multiple windows are saved in the same millisecond.
    """
    if window.shape != (WINDOW_SIZE, N_FEATURES):
        raise ValueError(f"Window shape {window.shape} != ({WINDOW_SIZE}, {N_FEATURES})")

    # Sanitize label to prevent path traversal
    safe_label = re.sub(r'[^a-zA-Z0-9_-]', '_', label)
    label_dir = os.path.join(output_dir, safe_label)
    os.makedirs(label_dir, exist_ok=True)

    ts_str   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    filepath = os.path.join(label_dir, f"{ts_str}_{window_index:04d}.csv")

    try:
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp"] + FEATURE_COLS)
            for i, row in enumerate(window):
                timestamp_ms = round(i * ROW_INTERVAL_MS, 3)
                writer.writerow([timestamp_ms] + [round(float(v), 6) for v in row])
    except IOError as e:
        logging.error(f"Failed to write window to {filepath}: {e}")
        raise

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

    FIX #3/#12: use buffer.total_written (monotonically increasing) instead of
    buffer.n_rows (saturates at capacity=1600 after the first 4 seconds) as the
    recording-start watermark.  With n_rows, any session started after warmup
    computed new_rows = 1600 - 1600 = 0 and captured a single row, silently
    producing 0–4 windows instead of the expected 60 for a 30-second session.

    With total_written, new_rows = total_written_end - total_written_start
    always equals the exact number of samples produced during the recording,
    regardless of how many times the ring has wrapped.
    """
    n_rows_needed = duration_seconds * SAMPLE_RATE_HZ

    print(f"\n[Capture] Label: {label.upper()}")
    print(f"[Capture] Target: {n_rows_needed} rows ({duration_seconds}s @ {SAMPLE_RATE_HZ}Hz)")
    print(f"[Capture] Starting in {countdown_seconds}s — set motor state now.")
    for i in range(countdown_seconds, 0, -1):
        print(f"          {i}...", flush=True)
        time.sleep(1)
    print("[Capture] RECORDING", flush=True)

    # FIX #3/#12: watermark using the monotonic total_written counter
    record_start_written = buffer.total_written
    record_start_time    = time.perf_counter()

    # Poll until the buffer has accumulated n_rows_needed NEW rows since start
    deadline = record_start_time + duration_seconds + 2.0  # +2s grace
    while True:
        new_rows = buffer.total_written - record_start_written
        if new_rows >= n_rows_needed:
            break
        if time.perf_counter() > deadline:
            print("[Capture] WARNING: buffer did not fill in time. Saving what we have.")
            break
        time.sleep(0.05)

    # Freeze the number of NEW rows available BEFORE taking the snapshot.
    # This prevents the ingest thread from adding rows between the count check
    # and the snapshot, which would cause the slice to drift into pre-session data.
    rows_to_slice = min(
        buffer.total_written - record_start_written,
        n_rows_needed,
    )

    snap = buffer.get_snapshot()

    # The most recent rows_to_slice rows in the snapshot are the recording
    data = snap[-max(rows_to_slice, 1):].astype(np.float32)

    if len(data) < n_rows_needed:
        print(
            f"[Capture] WARNING: captured {len(data)} rows "
            f"(expected {n_rows_needed}). "
            f"{'Proceeding with partial data.' if len(data) >= WINDOW_SIZE else 'Not enough data for even one window — aborting.'}"
        )
    if len(data) < WINDOW_SIZE:
        raise ValueError(
            f"[Capture] Captured only {len(data)} rows — minimum is "
            f"{WINDOW_SIZE} (one window). Check that the sensor firmware is "
            f"running and the ingest transport (UDP port / Bridge IPC FIFO) "
            f"is active and receiving data."
        )

    windows = slice_windows(data)
    paths = [
        save_window_as_csv(w, label=label, output_dir=output_dir, window_index=i)
        for i, w in enumerate(windows)
    ]

    print(f"[Capture] Done. {len(paths)} windows -> {output_dir}/{label}/")
    return paths
