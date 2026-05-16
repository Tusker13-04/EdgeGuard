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
from src.schema import N_FEATURES, WINDOW_SIZE, SAMPLE_RATE_HZ

log = logging.getLogger(__name__)

MODEL_PATH  = os.path.join(os.path.dirname(__file__), "..", "model", "edgeguard.onnx")
CLASS_NAMES = ["normal", "imbalance"]

# At LIS3DH +/-8g range (78.4 m/s^2 full scale), 12.0 m/s^2 (~1.2g) is below
# typical idle motor vibration and produces near-100% false positives.
# 40.0 m/s^2 (~4g) is a calibrated starting point for imbalance detection;
# adjust based on baseline vibration measurements for the specific motor.
RMS_ANOMALY_THRESHOLD = float(os.environ.get("EDGEGUARD_RMS_THRESHOLD", "40.0"))

# Max consecutive ONNX failures before the session is disabled
_ONNX_FAIL_LIMIT = 5

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

    FIX #4 (fallback dilution): slice snapshot[-WINDOW_SIZE:] so RMS is
    computed over the same 0.5-second window used by the ONNX path.
    Previously snapshot[:] was passed, averaging a spike across the full
    4-second history and suppressing it below the detection threshold.
    """
    xyz   = snapshot[-WINDOW_SIZE:, ACCEL_COLS]   # (200, 3) — most recent 0.5 s
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


class InferencePipeline:
    """
    Stateful inference runner.

    Tracks consecutive ONNX failures and disables sess after _ONNX_FAIL_LIMIT
    failures to stop the ERROR log flood on the QRB2210's eMMC.
    Send SIGHUP (or restart the process) after deploying a new model.
    """

    def __init__(self, sess=None):
        self.sess             = sess
        self._onnx_fail_count = 0

    def run_cycle(self, buffer: FastCircularBuffer) -> dict:
        """
        Single inference cycle.  Returns a result dict for the dashboard.
        All inference exceptions are caught so the calling loop is never killed.
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
            if self.sess is not None:
                result = _onnx_score(self.sess, snapshot)
                self._onnx_fail_count = 0   # reset on success
            else:
                result = _rule_based_score(snapshot)
        except Exception as exc:
            self._onnx_fail_count += 1
            if self._onnx_fail_count >= _ONNX_FAIL_LIMIT:
                log.critical(
                    "[Inference] ONNX failed %d consecutive times (%s). "
                    "Disabling ONNX session — falling back to rule-based permanently. "
                    "Redeploy model and restart to re-enable.",
                    self._onnx_fail_count, exc,
                )
                self.sess = None
                self._onnx_fail_count = 0
            else:
                log.error(
                    "[Inference] ONNX cycle failed (%s) — falling back to rule-based "
                    "(failure %d/%d).",
                    exc, self._onnx_fail_count, _ONNX_FAIL_LIMIT,
                )
            result = _rule_based_score(snapshot)

        latency_inference_ms = (time.perf_counter() - t1) * 1000
        result["latency_ms"] = round(latency_snapshot_ms + latency_inference_ms, 2)
        result["n_rows"]     = WINDOW_SIZE
        return result


# ---------------------------------------------------------------------------
# Module-level convenience functions (used by main.py and capture_session.py)
# ---------------------------------------------------------------------------

def load_model():
    """Public entry point: load the model once at startup."""
    return _load_model()


def run_inference_cycle(buffer: FastCircularBuffer, sess=None) -> dict:
    """
    Stateless convenience wrapper retained for backward compatibility.
    Prefer InferencePipeline.run_cycle() for production use.
    """
    pipeline = InferencePipeline(sess=sess)
    return pipeline.run_cycle(buffer)
