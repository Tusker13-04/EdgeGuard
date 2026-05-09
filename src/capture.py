import os
import csv
import time
import numpy as np
from datetime import datetime, timezone

WINDOW_SIZE = 500
FEATURE_NAMES = ["accX", "accY", "accZ", "temp"]

def slice_windows(data: np.ndarray, window_size: int = WINDOW_SIZE):
    n_windows = len(data) // window_size
    return [data[i * window_size:(i + 1) * window_size] for i in range(n_windows)]

def save_window_as_csv(window: np.ndarray, label: str, output_dir: str) -> str:
    label_dir = os.path.join(output_dir, label)
    os.makedirs(label_dir, exist_ok=True)
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    filepath = os.path.join(label_dir, f"{timestamp_str}.csv")
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp"] + FEATURE_NAMES)
        for i, row in enumerate(window):
            writer.writerow([i] + [round(float(v), 6) for v in row])
    return filepath

def record_session(
    buffer,
    label: str,
    output_dir: str,
    duration_seconds: int = 30,
    countdown_seconds: int = 5,
    sample_rate: int = 1000
):
    print(f"\n>>> Recording: {label.upper()} — Starting in {countdown_seconds} seconds.")
    print("    Prepare motor state now.")
    for i in range(countdown_seconds, 0, -1):
        print(f"    {i}...", flush=True)
        time.sleep(1)
    print("    RECORDING...", flush=True)

    n_rows = duration_seconds * sample_rate
    captured_rows = []
    start = time.perf_counter()
    while len(captured_rows) < n_rows:
        snap = buffer.get_snapshot()
        if len(snap) > 0:
            captured_rows = list(snap)
        time.sleep(0.001)

    data = np.array(captured_rows[-n_rows:], dtype=np.float32)
    windows = slice_windows(data, window_size=WINDOW_SIZE)
    saved_paths = [save_window_as_csv(w, label=label, output_dir=output_dir) for w in windows]
    elapsed = time.perf_counter() - start
    print(f"    Done. {len(saved_paths)} windows saved to {output_dir}/{label}/  ({elapsed:.1f}s)")
    return saved_paths
