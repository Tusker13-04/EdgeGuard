"""
src/inference.py  —  EdgeGuard DiagnosticEngine

Phase 2: Multimodal Fusion
  The DiagnosticEngine combines ONNX vibration model output with
  board_temp_c to produce a structured diagnostic string rather than
  a bare label.

Fusion matrix (derived from industrial bearing failure modes):
  imbalance + rising  -> Lubrication Failure — High Temp + High Vibration
  imbalance + normal  -> Mechanical Imbalance — check shaft alignment
  bearing   + rising  -> Bearing Failure Imminent — schedule maintenance
  bearing   + normal  -> Bearing Wear — monitor closely
  looseness + rising  -> Structural Looseness + Thermal Stress — urgent inspection
  looseness + normal  -> Structural Looseness — inspect mountings
  normal    + rising  -> Thermal Anomaly — check cooling / lubrication system
  normal    + normal  -> Nominal Operation
"""

from __future__ import annotations

import os
import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

TEMP_RISING_C = float(os.getenv("EG_TEMP_RISING_C", "55.0"))
INFERENCE_INTERVAL_S = 0.5

_FUSION: dict[tuple[str, str], str] = {
    ("imbalance", "rising"):  "Lubrication Failure — High Temp + High Vibration",
    ("imbalance", "normal"): "Mechanical Imbalance — check shaft alignment",
    ("bearing",   "rising"):  "Bearing Failure Imminent — schedule maintenance",
    ("bearing",   "normal"): "Bearing Wear — monitor closely",
    ("looseness", "rising"):  "Structural Looseness + Thermal Stress — urgent inspection",
    ("looseness", "normal"): "Structural Looseness — inspect mountings",
    ("normal",    "rising"):  "Thermal Anomaly — check cooling / lubrication system",
    ("normal",    "normal"): "Nominal Operation",
}


@dataclass
class DiagnosticResult:
    label: str
    imbalance_prob: float
    temp_state: str
    diagnostic: str
    confidence: float
    raw_probs: dict[str, float] = field(default_factory=dict)


class DiagnosticEngine:
    """
    Phase 2 inference engine with multimodal fusion.

    Usage:
        engine = DiagnosticEngine(onnx_path="model.onnx")
        result = engine.run(samples_np, board_temp_c=62.3)
    """

    CLASSES = ["normal", "imbalance", "bearing", "looseness"]

    def __init__(self, onnx_path: Optional[str] = None):
        self._session = None
        if onnx_path and os.path.exists(onnx_path):
            try:
                import onnxruntime as ort
                self._session = ort.InferenceSession(
                    onnx_path,
                    providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
                )
                logger.info("DiagnosticEngine: ONNX model loaded from %s", onnx_path)
            except Exception as exc:
                logger.warning("ONNX load failed (%s) — using RMS fallback", exc)
        else:
            logger.warning("DiagnosticEngine: no ONNX model found — using RMS fallback")

    def run(
        self,
        samples: np.ndarray,
        board_temp_c: float = 25.0,
    ) -> DiagnosticResult:
        probs = self._infer(samples)
        label = self.CLASSES[int(np.argmax(probs))]
        imbalance_prob = float(probs[self.CLASSES.index("imbalance")])
        temp_state = "rising" if board_temp_c >= TEMP_RISING_C else "normal"
        diagnostic = _FUSION.get((label, temp_state), "Unknown State")

        return DiagnosticResult(
            label=label,
            imbalance_prob=imbalance_prob,
            temp_state=temp_state,
            diagnostic=diagnostic,
            confidence=float(np.max(probs)),
            raw_probs=dict(zip(self.CLASSES, probs.tolist())),
        )

    def _infer(self, samples: np.ndarray) -> np.ndarray:
        if self._session is not None:
            return self._onnx_infer(samples)
        return self._rms_fallback(samples)

    def _onnx_infer(self, samples: np.ndarray) -> np.ndarray:
        inp_name = self._session.get_inputs()[0].name
        out_name = self._session.get_outputs()[0].name
        accel_only = samples[:, :3]
        flat = accel_only.flatten().astype(np.float32).reshape(1, -1)
        logits = self._session.run([out_name], {inp_name: flat})[0]
        e = np.exp(logits - logits.max())
        return (e / e.sum()).flatten()

    @staticmethod
    def _rms_fallback(samples: np.ndarray) -> np.ndarray:
        # Issue 5 fix: Compute RMS only over x, y, z columns
        rms = float(np.sqrt(np.mean(samples[:, :3] ** 2)))
        threshold = float(os.getenv("EG_RMS_THRESHOLD", "150.0"))
        if rms > threshold:
            return np.array([0.05, 0.85, 0.05, 0.05], dtype=np.float32)
        return np.array([0.90, 0.04, 0.03, 0.03], dtype=np.float32)
