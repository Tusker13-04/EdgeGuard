# src/inference.py
# Inference thread: runs at ~2Hz (every 500ms), takes a snapshot from the
# circular buffer, extracts features, and returns an anomaly score.
#
# Model loading:
#   - If MODEL_PATH exists: loads ONNX model with onnxruntime and runs it.
#   - If MODEL_PATH is absent: falls back to a rule-based RMS threshold.
#
# ONNX model contract (Edge Impulse export or custom):
#   Input  name : "input"    shape: (1, WINDOW_SIZE, N_FEATURES) float32
#   Output name : "output"   shape: (1, N_CLASSES)               float32
#   Classes     : ["normal", "imbalance"]  (index 1 = imbalance probability)

import os
import time
import logging
import numpy as np

from src.buffer import FastCircularBuffer, ACCEL_COLS
from src.udp_receiver import N_FEATURES
from src.capture import WINDOW_SIZE, SAMPLE_RATE_HZ

log = logging.getLogger(__name__)

MODEL_PATH  = os.path.join(os.path.dirname(__file__), "..", "model", "edgeguard.onnx")
CLASS_NAMES = ["normal", "imbalance"]

# Rule-based fallback: flag if XYZ RMS exceeds this threshold (m/s^2)
RMS_ANOMALY_THRESHOLD = 12.0

# Inference runs every INFERENCE_INTERVAL_S seconds
INFERENCE_INTERVAL_S = 0.5  # 2 Hz


def _load_model():
    """Try to load the ONNX model. Returns session or None."""
    model_path = os.path.abspath(MODEL_PATH)
    if not os.path.isfile(model_path):
        log.warning("[Inference] No model found at %s — using rule-based fallback.", model_path)
        return None
    try:
        import onnxruntime as ort
        sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        inp = sess.get_inputs()[0]
        log.info(
            "[Inference] ONNX model loaded from %s — input: %s %s",
            model_path, inp.name, inp.shape,
        )
        return sess
    except Exception as e:
        log.error("[Inference] Failed to load ONNX model: %s — using fallback.", e)
        return None


def _rule_based_score(snapshot: np.ndarray) -> dict:
    """
    Fallback when no ONNX model is available.
    Uses ACCEL_COLS constant so column order is documented centrally in buffer.py.
    """
    xyz   = snapshot[:, ACCEL_COLS]   # columns 0,1,2 = accel_x, accel_y, accel_z
    rms   = float(np.sqrt(np.mean(xyz ** 2)))
    score = min(1.0, rms / RMS_ANOMALY_THRESHOLD)
    label = CLASS_NAMES[1] if score > 0.5 else CLASS_NAMES[0]
    return {
        "label":          label,
        "imbalance_prob": round(score, 4),
        "normal_prob":    round(1.0 - score, 4),
        "source":         "rule_based",
    }


def _onnx_score(sess, snapshot: np.ndarray) -> dict:
    """
    Run one inference pass with the ONNX model.
    Raises on shape mismatch — caller wraps in try/except.
    """
    window = snapshot[-WINDOW_SIZE:].astype(np.float32)   # (200, 4)
    x = window[np.newaxis, ...]                            # (1, 200, 4)
    input_name  = sess.get_inputs()[0].name
    output_name = sess.get_outputs()[0].name
    probs = sess.run([output_name], {input_name: x})[0][0]  # (n_classes,)
    idx   = int(np.argmax(probs))
    return {
        "label":          CLASS_NAMES[idx] if idx < len(CLASS_NAMES) else str(idx),
        "imbalance_prob": round(float(probs[1]) if len(probs) > 1 else float(probs[0]), 4),
        "normal_prob":    round(float(probs[0]), 4),
        "source":         "onnx",
    }


def run_inference_cycle(buffer: FastCircularBuffer, sess=None) -> dict:
    """
    Single inference cycle. Call this in a loop from the inference thread.
    Returns a result dict suitable for the dashboard.

    All inference exceptions are caught here so the calling loop is never
    killed by a transient model error (e.g. ORT shape mismatch on model swap).
    The rule-based fallback is used automatically on any ONNX error.
    """
    t0       = time.perf_counter()
    snapshot = buffer.get_snapshot()
    latency_snapshot_ms = (time.perf_counter() - t0) * 1000

    if len(snapshot) < WINDOW_SIZE:
        return {
            "label":          "buffering",
            "imbalance_prob": 0.0,
            "normal_prob":    0.0,
            "source":         "none",
            "latency_ms":     0.0,
            "n_rows":         len(snapshot),
        }

    t1 = time.perf_counter()
    try:
        if sess is not None:
            result = _onnx_score(sess, snapshot)
        else:
            result = _rule_based_score(snapshot)
    except Exception as exc:
        log.error(
            "[Inference] cycle failed (%s) — falling back to rule-based.", exc
        )
        result = _rule_based_score(snapshot)

    latency_inference_ms = (time.perf_counter() - t1) * 1000
    result["latency_ms"] = round(latency_snapshot_ms + latency_inference_ms, 2)
    result["n_rows"]     = WINDOW_SIZE
    return result


def load_model():
    """Public entry point for the main pipeline to load the model once at startup."""
    return _load_model()
