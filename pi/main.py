#!/usr/bin/env python3
"""
EdgeGuard — pi/main.py
======================
Production-grade inference node for the Raspberry Pi.

Responsibilities
----------------
1. Thread-A  (UDP ingestion, "fast path")
   - Blocks on socket.recvfrom() at OS level — zero busy-wait.
   - Unpacks the 28-byte binary struct sent by the ESP8266 firmware.
   - Validates sequence-ID monotonicity to detect and count dropped packets.
   - Measures inter-arrival jitter with a rolling window.
   - Writes one row directly into the pre-allocated NumPy circular buffer.
   - Acquires the shared lock for the minimum possible time (single index bump).

2. Thread-B  (ML inference, "slow path", 2 Hz)
   - Wakes every INFER_PERIOD_S seconds.
   - Snapshots the circular buffer under a brief lock (copy-out only).
   - Unwraps the circular buffer into chronological order — critical for
     LSTM/1D-CNN models that depend on temporal sequence.
   - Runs preprocessing (per-feature z-score normalisation) using
     pre-allocated output arrays — no heap allocation in the hot path.
   - Runs ONNX Runtime inference.
   - Computes an anomaly score from the probability distribution.
   - Emits a fully-structured JSON line to stdout (read by server.py).

Stdout contract (one JSON object per line, no trailing commas)
--------------  see README.md §stdout JSON schema

Usage
-----
    python pi/main.py --model pi/model.onnx [--udp-host 0.0.0.0] [--udp-port 5005]
                      [--fs 500] [--window 500] [--infer-hz 2]

Note: if no model file is found, the node runs in DEMO mode — it synthesises
sinusoidal vibration data so the dashboard and server.py can be tested without
physical hardware.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import struct
import sys
import threading
import time
from collections import deque
from typing import Dict, List, Optional

import numpy as np

# ── Optional imports (gracefully absent on dev machines) ──────────────────────
try:
    import onnxruntime as ort
    _ORT_AVAILABLE = True
except ImportError:
    _ORT_AVAILABLE = False

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# Constants & defaults
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_UDP_HOST   = "0.0.0.0"
DEFAULT_UDP_PORT   = 5005
DEFAULT_FS         = 500          # nominal sampling frequency (Hz)
DEFAULT_WINDOW     = 500          # inference window length (samples)
DEFAULT_INFER_HZ   = 2            # how often to run inference
DEFAULT_BUF_MULT   = 4            # circular buffer = BUF_MULT × window
JITTER_WINDOW      = 64           # samples for rolling jitter estimate
ANOMALY_PERCENTILE = 95           # percentile of class probs for anomaly score

# Binary struct layout — MUST match firmware/edgeguard.ino SensorPayload
# < = little-endian, I = uint32, f = float32
# Fields: timestamp_us, seq_id, accel_x, accel_y, accel_z, temp, current
STRUCT_FMT  = "<IIffffff"
STRUCT_SIZE = struct.calcsize(STRUCT_FMT)   # → 32 bytes
# Note: firmware sends 7 floats: accelX,Y,Z,temp,current — 4+4+4+4+4+4+4 = 28
# We parse 2 uint32 + 5 float = 28 bytes.  Keep in sync.
STRUCT_FMT  = "<IIfffff"   # ts_us(4) + seq(4) + aX(4) + aY(4) + aZ(4) + temp(4) + curr(4)
STRUCT_SIZE = struct.calcsize(STRUCT_FMT)  # = 28 bytes exactly

# Feature column indices in the buffer (per-row)
COL_AX   = 0
COL_AY   = 1
COL_AZ   = 2
COL_TEMP = 3
COL_CURR = 4
N_FEATURES = 5

# ─────────────────────────────────────────────────────────────────────────────
# Shared state — all cross-thread data lives here
# ─────────────────────────────────────────────────────────────────────────────
class SharedState:
    """Single owner of all mutable state shared between Thread-A and Thread-B."""
    def __init__(self, buf_size: int) -> None:
        self.lock = threading.Lock()

        # ── Circular buffer (pre-allocated, never reallocated) ────────────────
        # Shape: (buf_size, N_FEATURES).  float32 to match ONNX input dtype.
        self.buffer   = np.zeros((buf_size, N_FEATURES), dtype=np.float32)
        self.buf_size = buf_size
        self.write_idx = 0          # next slot to write into
        self.total_written = 0      # monotonic counter (not wrapped)

        # ── Pipeline metrics (updated by Thread-A) ───────────────────────────
        self.packets_rx      = 0
        self.packets_dropped = 0
        self.last_seq_id     = -1
        self.last_seq_gap    = 0
        # Rolling jitter: store last N inter-arrival times (microseconds)
        self._arrival_times: deque = deque(maxlen=JITTER_WINDOW)
        self.jitter_us       = 0.0
        self.measured_hz     = 0.0

    def write_row(self, row: np.ndarray) -> None:
        """Write one sample row into the circular buffer.  Lock-free — caller
        must hold self.lock for this single operation."""
        self.buffer[self.write_idx] = row
        self.write_idx = (self.write_idx + 1) % self.buf_size
        self.total_written += 1

    def snapshot(self, window: int) -> np.ndarray:
        """Return a chronologically-ordered copy of the last `window` samples.
        Acquires the lock for the minimum duration — only the np.empty + index
        capture happen under lock; the actual copy is done outside.
        """
        with self.lock:
            wi   = self.write_idx
            buf  = self.buffer        # reference, not copy
            total = self.total_written

        if total < window:
            # Buffer not yet filled; return zero-padded window
            out = np.zeros((window, N_FEATURES), dtype=np.float32)
            valid = min(total, self.buf_size)
            # newest `valid` samples end at wi (exclusive)
            if valid > 0:
                idxs = [(wi - valid + i) % self.buf_size for i in range(valid)]
                out[-valid:] = buf[idxs]
            return out

        # Unwrap circular buffer → chronological order
        # Oldest sample: (wi) — newest sample: (wi-1)
        idx_old = wi                         # oldest slot
        if idx_old + window <= self.buf_size:
            return buf[idx_old:idx_old + window].copy()
        else:
            tail = self.buf_size - idx_old
            head = window - tail
            out  = np.empty((window, N_FEATURES), dtype=np.float32)
            out[:tail] = buf[idx_old:]
            out[tail:] = buf[:head]
            return out


# ─────────────────────────────────────────────────────────────────────────────
# Thread-A — UDP ingestion
# ─────────────────────────────────────────────────────────────────────────────
def udp_ingestion_thread(state: SharedState, host: str, port: int) -> None:
    """Blocks on recvfrom(). Parses binary payload, writes to circular buffer.
    This thread does NO ML work — kept as lean as possible."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)  # 256 KB kernel buf
    sock.bind((host, port))

    row = np.zeros(N_FEATURES, dtype=np.float32)  # reusable row — no alloc per packet
    last_arrival_us = 0

    while True:
        try:
            data, _ = sock.recvfrom(64)   # 28-byte payload; 64 gives headroom
        except OSError:
            continue

        if len(data) < STRUCT_SIZE:
            continue   # malformed / short packet

        # ── Parse ───────────────────────────────────────────────────────────
        ts_us, seq, ax, ay, az, temp, curr = struct.unpack_from(STRUCT_FMT, data)

        now_us = time.monotonic_ns() // 1_000   # host arrival time in µs

        # ── Metrics (no lock needed for reads used only in this thread) ────
        with state.lock:
            state.packets_rx += 1

            # Sequence gap detection
            if state.last_seq_id >= 0:
                gap = seq - state.last_seq_id - 1
                if gap > 0:
                    state.packets_dropped += gap
                state.last_seq_gap = gap
            state.last_seq_id = seq

            # Jitter: rolling σ of inter-arrival intervals
            if last_arrival_us > 0:
                interval = now_us - last_arrival_us
                state._arrival_times.append(interval)
                if len(state._arrival_times) >= 2:
                    state.jitter_us  = float(np.std(state._arrival_times))
                    mean_interval    = float(np.mean(state._arrival_times))
                    state.measured_hz = 1_000_000.0 / mean_interval if mean_interval > 0 else 0.0

            # Write features into circular buffer
            row[COL_AX]   = ax
            row[COL_AY]   = ay
            row[COL_AZ]   = az
            row[COL_TEMP] = temp
            row[COL_CURR] = curr
            state.write_row(row)

        last_arrival_us = now_us


# ─────────────────────────────────────────────────────────────────────────────
# Demo mode — synthetic vibration generator (no hardware required)
# ─────────────────────────────────────────────────────────────────────────────
def demo_ingestion_thread(state: SharedState, fs: int) -> None:
    """Synthesises sinusoidal + noise vibration at `fs` Hz.
    State toggles between NORMAL and IMBALANCE every 15 s so the dashboard
    demonstrates a full fault-detection cycle without physical hardware."""
    period     = 1.0 / fs
    seq        = 0
    t          = 0.0
    phase_flip = 0     # 0 = normal, 1 = imbalance
    flip_every = 15.0  # seconds
    next_flip  = time.monotonic() + flip_every

    row = np.zeros(N_FEATURES, dtype=np.float32)

    while True:
        now = time.monotonic()
        if now >= next_flip:
            phase_flip ^= 1
            next_flip   = now + flip_every

        # Normal: low-amplitude broadband noise
        # Imbalance: strong 1× shaft frequency (10 Hz) + harmonics
        if phase_flip == 0:
            ax = np.random.normal(0.0, 0.05)
            ay = np.random.normal(0.0, 0.05)
            az = 9.81 + np.random.normal(0.0, 0.03)
        else:
            ax = 0.8 * math.sin(2 * math.pi * 10 * t) + np.random.normal(0.0, 0.05)
            ay = 0.3 * math.sin(2 * math.pi * 20 * t) + np.random.normal(0.0, 0.05)
            az = 9.81 + 0.2 * math.sin(2 * math.pi * 10 * t) + np.random.normal(0.0, 0.02)

        temp = 38.0 + phase_flip * 4.0 + np.random.normal(0.0, 0.2)
        curr = 0.5 + phase_flip * 0.3 + np.random.normal(0.0, 0.02)

        with state.lock:
            state.packets_rx += 1
            state.last_seq_id  = seq
            state.last_seq_gap = 0
            state._arrival_times.append(int(period * 1_000_000))
            state.jitter_us   = 50.0   # realistic demo jitter
            state.measured_hz = float(fs)
            row[COL_AX]   = ax
            row[COL_AY]   = ay
            row[COL_AZ]   = az
            row[COL_TEMP] = temp
            row[COL_CURR] = curr
            state.write_row(row)

        seq += 1
        t   += period
        time.sleep(period)


# ─────────────────────────────────────────────────────────────────────────────
# Preprocessing
# ─────────────────────────────────────────────────────────────────────────────
# Pre-allocate the normalised window — never allocate inside the inference loop
_norm_window: Optional[np.ndarray] = None

def preprocess(window: np.ndarray) -> np.ndarray:
    """Per-feature z-score normalisation.  Uses pre-allocated output array.
    Clamps σ to 1e-6 to avoid division-by-zero on flat signals."""
    global _norm_window
    if _norm_window is None or _norm_window.shape != window.shape:
        _norm_window = np.empty_like(window)
    mu  = window.mean(axis=0, keepdims=True)           # (1, N_FEATURES)
    sig = window.std( axis=0, keepdims=True).clip(1e-6)
    np.subtract(window, mu,  out=_norm_window)
    np.divide(  _norm_window, sig, out=_norm_window)
    return _norm_window


# ─────────────────────────────────────────────────────────────────────────────
# Anomaly score — model-agnostic, works even if model has no anomaly head
# ─────────────────────────────────────────────────────────────────────────────
def compute_anomaly_score(probs: Dict[str, float]) -> float:
    """Entropy-based anomaly: high entropy = uncertain = anomalous.
    Returns a value in [0, 1]; 0 = totally confident, 1 = max uncertainty."""
    vals  = np.array(list(probs.values()), dtype=np.float64)
    vals  = np.clip(vals, 1e-9, 1.0)
    H     = -np.sum(vals * np.log(vals))          # Shannon entropy
    H_max = math.log(max(len(vals), 2))           # max possible entropy
    return float(H / H_max)


DEFAULT_ANOMALY_THRESHOLD = 0.35   # calibrate during training


# ─────────────────────────────────────────────────────────────────────────────
# Thread-B — ONNX inference
# ─────────────────────────────────────────────────────────────────────────────
def inference_thread(
    state:      SharedState,
    session:    Optional["ort.InferenceSession"],
    class_names: List[str],
    window:     int,
    infer_period: float,
    start_time: float,
    demo_mode:  bool,
) -> None:
    """Wakes every infer_period seconds, snapshots buffer, runs ONNX, emits JSON."""

    input_name  = session.get_inputs()[0].name  if session else "input"
    output_name = session.get_outputs()[0].name if session else "output"

    # Pre-allocate ONNX input tensor shape: (1, window, N_FEATURES)
    ort_input   = np.empty((1, window, N_FEATURES), dtype=np.float32)

    infer_count  = 0
    infer_times  = deque(maxlen=20)   # rolling inference latency (ms)
    cycle_start  = time.monotonic()

    while True:
        cycle_start = time.monotonic()

        # ── 1. Snapshot buffer (brief lock) ──────────────────────────────────
        t0 = time.perf_counter()
        raw_window = state.snapshot(window)
        snapshot_ms = (time.perf_counter() - t0) * 1000.0

        # ── 2. Preprocess ────────────────────────────────────────────────────
        t1 = time.perf_counter()
        norm = preprocess(raw_window)   # (window, N_FEATURES), in-place on pre-alloc
        preprocess_ms = (time.perf_counter() - t1) * 1000.0

        # ── 3. Inference ─────────────────────────────────────────────────────
        t2 = time.perf_counter()
        if session is not None:
            np.copyto(ort_input[0], norm)          # zero-copy fill
            raw_out = session.run([output_name], {input_name: ort_input})[0]
            # raw_out shape: (1, n_classes) — softmax probabilities
            probs_arr = raw_out[0].astype(np.float64)
        else:
            # Demo mode: synthesise plausible probabilities based on signal RMS
            rms_ax = float(np.sqrt(np.mean(norm[:, COL_AX] ** 2)))
            p_imbalance = min(max((rms_ax - 0.5) / 2.0, 0.0), 0.95)
            probs_arr   = np.array([1.0 - p_imbalance, p_imbalance])

        infer_ms = (time.perf_counter() - t2) * 1000.0
        infer_times.append(infer_ms)

        # ── 4. Build outputs ─────────────────────────────────────────────────
        probs       = {cls: float(p) for cls, p in zip(class_names, probs_arr)}
        best_idx    = int(np.argmax(probs_arr))
        state_label = class_names[best_idx]
        confidence  = float(probs_arr[best_idx])

        anomaly_score = compute_anomaly_score(probs)

        infer_count += 1
        cycle_ms     = (time.monotonic() - cycle_start) * 1000.0
        infer_hz     = 1.0 / infer_period

        # ── 5. Read pipeline metrics (brief lock) ────────────────────────────
        with state.lock:
            pkts_rx      = state.packets_rx
            pkts_drop    = state.packets_dropped
            jitter_us    = state.jitter_us
            last_gap     = state.last_seq_gap
            measured_hz  = state.measured_hz
            wi           = state.write_idx
            buf_size     = state.buf_size
            last_temp    = float(state.buffer[(wi - 1) % buf_size, COL_TEMP])

        drop_rate = (pkts_drop / max(pkts_rx, 1)) * 100.0

        # ── 6. System metrics ────────────────────────────────────────────────
        cpu_pct = psutil.cpu_percent()   if _PSUTIL_AVAILABLE else 0.0
        ram_pct = psutil.virtual_memory().percent if _PSUTIL_AVAILABLE else 0.0
        uptime_s = time.monotonic() - start_time

        # ── 7. Emit JSON line to stdout (server.py reads this) ────────────────
        frame = {
            "inference": {
                "state":             state_label,
                "confidence":        round(confidence, 4),
                "probabilities":     {k: round(v, 4) for k, v in probs.items()},
                "anomaly_score":     round(anomaly_score, 5),
                "anomaly_threshold": DEFAULT_ANOMALY_THRESHOLD,
                "infer_hz":          round(infer_hz, 2),
                "latency_ms":        round(infer_ms, 2),
                "preprocess_ms":     round(preprocess_ms, 3),
                "snapshot_ms":       round(snapshot_ms, 3),
                "cycle_ms":          round(cycle_ms, 2),
            },
            "pipeline": {
                "packets_rx":      pkts_rx,
                "packets_dropped": pkts_drop,
                "drop_rate_pct":   round(drop_rate, 4),
                "jitter_us":       round(jitter_us, 2),
                "last_seq_gap":    last_gap,
                "measured_hz":     round(measured_hz, 2),
            },
            "buffer": {
                "size":        buf_size,
                "write_index": wi,
            },
            "sensor": {
                "temp_c": round(last_temp, 2),
            },
            "system": {
                "cpu_pct":   round(cpu_pct, 1),
                "ram_pct":   round(ram_pct, 1),
                "uptime_s":  round(uptime_s, 1),
                "ws_clients": 0,    # updated live by server.py via shared counter
                "demo_mode": demo_mode,
            },
        }

        # json.dumps is fast enough; orjson not needed here (server.py uses it)
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()

        # ── 8. Precise sleep to hit infer_period ─────────────────────────────
        elapsed = time.monotonic() - cycle_start
        sleep_for = infer_period - elapsed
        if sleep_for > 0:
            time.sleep(sleep_for)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="EdgeGuard inference node")
    parser.add_argument("--model",     default="pi/model.onnx",   help="Path to ONNX model")
    parser.add_argument("--udp-host",  default=DEFAULT_UDP_HOST,  help="UDP listen address")
    parser.add_argument("--udp-port",  type=int, default=DEFAULT_UDP_PORT, help="UDP port")
    parser.add_argument("--fs",        type=int, default=DEFAULT_FS,      help="Nominal sampling Hz")
    parser.add_argument("--window",    type=int, default=DEFAULT_WINDOW,  help="Inference window samples")
    parser.add_argument("--infer-hz",  type=float, default=DEFAULT_INFER_HZ, help="Inference rate (Hz)")
    parser.add_argument("--classes",   default="normal,imbalance", help="Comma-separated class names")
    parser.add_argument("--demo",      action="store_true", help="Force demo (synthetic) mode")
    args = parser.parse_args()

    class_names   = [c.strip() for c in args.classes.split(",")]
    infer_period  = 1.0 / args.infer_hz
    buf_size      = args.window * DEFAULT_BUF_MULT
    demo_mode     = args.demo
    session       = None

    # ── Load ONNX model ───────────────────────────────────────────────────────
    if not demo_mode and os.path.isfile(args.model):
        if not _ORT_AVAILABLE:
            print(
                "[EdgeGuard] ERROR: onnxruntime not installed. "
                "Run: pip install onnxruntime  or use --demo flag.",
                file=sys.stderr,
            )
            sys.exit(1)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2          # conservative for Pi
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(args.model, sess_options=opts)
        print(f"[EdgeGuard] Model loaded: {args.model}", file=sys.stderr)
        # Override class names from model metadata if present
        meta = session.get_modelmeta().custom_metadata_map
        if "classes" in meta:
            class_names = [c.strip() for c in meta["classes"].split(",")]
    else:
        demo_mode = True
        print(
            "[EdgeGuard] DEMO MODE — no model file found. "
            "Synthesising vibration data.  Pass --model to use a real model.",
            file=sys.stderr,
        )

    state      = SharedState(buf_size=buf_size)
    start_time = time.monotonic()

    # ── Start Thread-A ────────────────────────────────────────────────────────
    if demo_mode:
        t_ingest = threading.Thread(
            target=demo_ingestion_thread,
            args=(state, args.fs),
            daemon=True,
            name="demo-ingestion",
        )
    else:
        t_ingest = threading.Thread(
            target=udp_ingestion_thread,
            args=(state, args.udp_host, args.udp_port),
            daemon=True,
            name="udp-ingestion",
        )
    t_ingest.start()
    print(
        f"[EdgeGuard] Ingestion thread started  "
        f"({'demo' if demo_mode else f'UDP {args.udp_host}:{args.udp_port}'})",
        file=sys.stderr,
    )

    # ── Start Thread-B ────────────────────────────────────────────────────────
    t_infer = threading.Thread(
        target=inference_thread,
        args=(
            state, session, class_names,
            args.window, infer_period, start_time, demo_mode,
        ),
        daemon=True,
        name="onnx-inference",
    )
    t_infer.start()
    print(
        f"[EdgeGuard] Inference thread started  "
        f"(window={args.window} samples, {args.infer_hz} Hz)",
        file=sys.stderr,
    )

    # Main thread just keeps the process alive
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("[EdgeGuard] Shutting down.", file=sys.stderr)


if __name__ == "__main__":
    main()
