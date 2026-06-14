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
import os
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from collections import deque
from typing import Optional

from .inference import DiagnosticEngine, DiagnosticResult

logger = logging.getLogger(__name__)

# AWS Greengrass V2 setup
try:
    import awsiot.greengrasscoreipc.clientv2 as clientv2
    from awsiot.greengrasscoreipc.model import QOS
    import boto3
    ipc_client = clientv2.GreengrassCoreIPCClientV2()
    logger.info("AWS Greengrass IPC client initialized")
except Exception as e:
    ipc_client = None
    QOS = None
    boto3 = None
    logger.warning("AWS Greengrass IPC client not available: %s", e)

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
        self._lock = threading.Lock()
        
        # Thread Pool for background I/O tasks
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="engine-io")

        # Start heartbeat thread
        self._stop_event = threading.Event()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()

        # OTA Shadow Setup
        if ipc_client:
            self._init_shadow_subscription()

    def shutdown(self):
        self._stop_event.set()
        self.executor.shutdown(wait=False)
        if self._hb_thread.is_alive():
            self._hb_thread.join(timeout=1.0)

    def _init_shadow_subscription(self):
        def on_shadow_delta(event):
            try:
                state = getattr(event, 'state', None)
                if state and "model_version" in state:
                    new_model = state["model_version"]
                    logger.info("OTA Update: Shadow delta requests model %s", new_model)
                    self.executor.submit(self._download_and_reload_model, new_model)
            except Exception as e:
                logger.error("Error in shadow delta callback: %s", e)

        try:
            # Subscribe to the named shadow 'EdgeGuardModelShadow'
            ipc_client.subscribe_to_shadow_state_updated(
                thing_name=os.environ.get("AWS_IOT_THING_NAME", ""),
                shadow_name="EdgeGuardModelShadow",
                on_stream_event=on_shadow_delta
            )
            logger.info("Subscribed to EdgeGuardModelShadow delta updates")
        except Exception as e:
            logger.error("Failed to subscribe to shadow updates: %s", e)

    def _download_and_reload_model(self, s3_key: str):
        if not boto3:
            logger.error("boto3 not available, cannot download model")
            return
        local_path = f"model/{os.path.basename(s3_key)}"
        logger.info("Downloading %s to %s via boto3 (TES)", s3_key, local_path)
        try:
            s3 = boto3.client("s3")
            bucket = "edgeguard-artifacts"
            s3.download_file(bucket, s3_key, local_path)
            logger.info("Download complete. Hot-reloading DiagnosticEngine.")
            with self._lock:
                self.diag = DiagnosticEngine(onnx_path=local_path)
        except Exception as e:
            logger.error("OTA Model Update failed: %s", e)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.bridge.put("heartbeat", "{}")
            except Exception as e:
                logger.error(f"Heartbeat failed: {e}")
            time.sleep(2.0)
            
    def _publish_mqtt(self, telemetry: dict) -> None:
        """Publish telemetry to AWS IoT Core via Greengrass IPC."""
        if ipc_client and QOS:
            try:
                ipc_client.publish_to_iot_core(
                    topic_name="edgeguard/telemetry",
                    qos=QOS.AT_LEAST_ONCE,
                    payload=json.dumps(telemetry).encode()
                )
            except Exception as e:
                logger.error("Failed to publish to AWS IoT Core: %s", e)

    def process_batch(
        self,
        samples_flat: list[float],
        board_temp_c: float = 25.0,
        source: str = "sensor_batch",
    ) -> dict:
        # Issue 4 fix: Reshape to 4 columns (x, y, z, temp) to match schema
        if len(samples_flat) % 4 != 0:
            raise ValueError(f"samples_flat length {len(samples_flat)} is not a multiple of 4")
        samples = np.array(samples_flat, dtype=np.float32).reshape(-1, 4)
        result: DiagnosticResult = self.diag.run(samples, board_temp_c=board_temp_c)

        # Energy waste estimation (0% to ~25% max waste based on imbalance)
        energy_waste_pct = min(25.0, result.imbalance_prob * 25.0)

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
            "energy_waste":   round(energy_waste_pct, 1),
        }

        logger.info(
            "[EdgeGuard] %s | diag=%r | prob=%.2f | temp=%.1f°C | energy_waste=%.1f%%",
            result.label, result.diagnostic, result.imbalance_prob, board_temp_c, energy_waste_pct
        )

        # Publish to AWS via ThreadPool
        self.executor.submit(self._publish_mqtt, telemetry)

        with self._lock:
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
                MIN_SAFE_THRESHOLD = 1000 # mg floor (1.0 g)
                avg_prob = sum(self._recent_probs) / len(self._recent_probs)
                optimal_threshold = max(
                    int(TUNE_NEW_THRESHOLD / (1.0 + avg_prob * NOISE_FLOOR_COEFF)),
                    MIN_SAFE_THRESHOLD
                )

                payload = json.dumps({"threshold": optimal_threshold}).encode()
                try:
                    self.bridge.put("remote_tune", payload)
                    logger.warning("[EdgeGuard] Remote tuned MPU threshold to %d", optimal_threshold)
                    self._tune_dispatched_at = now
                    self._recent_probs.clear()
                except Exception as e:
                    logger.error("Failed to dispatch remote tune: %s", e)
                logger.info(
                    "[EdgeGuard] Smart Remote-tune dispatched -> optimal_threshold=%d mg (avg_prob=%.2f)",
                    optimal_threshold, avg_prob,
                )

