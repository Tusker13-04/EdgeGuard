"""
src/engine.py  —  EdgeGuard MPU Pipeline

Phase 2+3 wiring:
  - Receives sensor_batch / anomaly_trigger from Bridge
  - Calls DiagnosticEngine for multimodal fusion
  - Emits rich telemetry: { label, diagnostic, imbalance_prob, confidence, board_temp_c, ... }
  - Implements remote-tune dispatch back to MCU when imbalance_prob trends high
"""

from __future__ import annotations

import json
import logging
import time
import threading
import numpy as np
from collections import deque
from typing import Optional

from .inference import DiagnosticEngine, DiagnosticResult

logger = logging.getLogger(__name__)

TUNE_PROB_THRESHOLD = 0.70
TUNE_WINDOW         = 5
TUNE_NEW_THRESHOLD  = 1500   # mg — tighter than the default 2000 mg


class EdgeGuardEngine:
    """
    Central MPU pipeline.  Instantiate once; call process_batch() per Bridge event.
    """

    def __init__(self, bridge, onnx_path: Optional[str] = None):
        self.bridge  = bridge
        self.diag    = DiagnosticEngine(onnx_path=onnx_path)
        self._recent_probs: deque[float] = deque(maxlen=TUNE_WINDOW)
        self._tune_dispatched_at: float = 0.0

        # Start heartbeat thread
        self._stop_event = threading.Event()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.bridge.put("heartbeat", "{}")
            except Exception as e:
                logger.error(f"Heartbeat failed: {e}")
            time.sleep(2.0)

    def process_batch(
        self,
        samples_flat: list[float],
        board_temp_c: float = 25.0,
        source: str = "sensor_batch",
    ) -> dict:
        # Issue 4 fix: Reshape to 4 columns (x, y, z, temp) to match schema
        samples = np.array(samples_flat, dtype=np.float32).reshape(-1, 4)
        result: DiagnosticResult = self.diag.run(samples, board_temp_c=board_temp_c)

        telemetry = {
            "ts":             time.time(),
            "source":         source,
            "label":          result.label,
            "diagnostic":     result.diagnostic,
            "imbalance_prob": round(result.imbalance_prob, 4),
            "confidence":     round(result.confidence, 4),
            "temp_state":     result.temp_state,
            "board_temp_c":   board_temp_c,
            "raw_probs":      result.raw_probs,
        }

        logger.info(
            "[EdgeGuard] %s | diag=%r | prob=%.2f | temp=%.1f°C",
            result.label, result.diagnostic, result.imbalance_prob, board_temp_c,
        )

        self._recent_probs.append(result.imbalance_prob)
        self._maybe_dispatch_remote_tune()

        return telemetry

    def _maybe_dispatch_remote_tune(self) -> None:
        if len(self._recent_probs) < TUNE_WINDOW:
            return
        if all(p >= TUNE_PROB_THRESHOLD for p in self._recent_probs):
            now = time.time()
            if now - self._tune_dispatched_at > 60.0:
                # ── Smart Threshold Tuning ─────────────────────────────────
                # Calculate optimal threshold based on current noise/probability
                #Higher prob means we need a tighter (lower) threshold on the MCU
                NOISE_FLOOR_COEFF = 1.5
                avg_prob = sum(self._recent_probs) / len(self._recent_probs)
                optimal_threshold = int(TUNE_NEW_THRESHOLD / (1.0 + avg_prob * NOISE_FLOOR_COEFF))

                payload = json.dumps({"threshold": optimal_threshold})
                self.bridge.put("remote_tune", payload)
                self._tune_dispatched_at = now
                self._recent_probs.clear()
                logger.info(
                    "[EdgeGuard] Smart Remote-tune dispatched -> optimal_threshold=%d mg (avg_prob=%.2f)",
                    optimal_threshold, avg_prob,
                )
