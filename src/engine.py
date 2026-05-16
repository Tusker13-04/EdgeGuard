# src/engine.py
# Pipeline orchestration engine for EdgeGuard.
#
# Encapsulates the multi-threaded ingest and inference loop,
# providing a unified interface for the CLI and dashboard.

import time
import json
import logging
import threading
import signal
import math
from typing import Optional

from src.schema import BaseReceiver
from src.buffer import FastCircularBuffer
from src.inference import InferencePipeline, load_model, INFERENCE_INTERVAL_S

log = logging.getLogger(__name__)

class PipelineEngine:
    """
    Orchestrates the EdgeGuard pipeline.
    
    Wires together an ingest receiver, a thread-safe circular buffer,
    and a stateful inference pipeline.
    """

    def __init__(self, receiver: BaseReceiver, interval: float = INFERENCE_INTERVAL_S):
        self.receiver   = receiver
        self.interval   = interval
        self.buf        = FastCircularBuffer()
        self.pipeline   = InferencePipeline(sess=load_model())
        self.stop_event = threading.Event()
        self._ingest_thread: Optional[threading.Thread] = None
        self._ingest_exc: list = [None]

    def _ingest_guarded(self):
        """Internal wrapper to catch ingest thread exceptions."""
        try:
            self.receiver.run(self.buf, self.stop_event)
        except Exception as exc:
            log.critical("[Engine] Ingest thread died: %s", exc)
            self._ingest_exc[0] = exc
            self.stop_event.set()

    def start(self):
        """Start the background ingest thread."""
        log.info("[Engine] Starting ingest: %s", self.receiver.__class__.__name__)
        self._ingest_thread = threading.Thread(target=self._ingest_guarded, daemon=True)
        self._ingest_thread.start()

    def stop(self):
        """Signal the pipeline to stop and wait for cleanup."""
        log.info("[Engine] Stopping pipeline...")
        self.stop_event.set()
        if self._ingest_thread:
            self._ingest_thread.join(timeout=2.0)

    def run_inference_loop(self):
        """
        Main inference loop. Runs until stop_event is set.
        Yields telemetry dicts for each cycle.
        """
        log.info("[Engine] Inference loop starting at %.1fHz", 1.0 / self.interval)
        next_tick = time.perf_counter()

        while not self.stop_event.is_set():
            if self._ingest_exc[0] is not None:
                break

            result = self.pipeline.run_cycle(self.buf)

            telemetry = {
                "ts":             round(time.time(), 3),
                "label":          result["label"],
                "imbalance_prob": result["imbalance_prob"],
                "normal_prob":    result["normal_prob"],
                "source":         result["source"],
                "latency_ms":     result["latency_ms"],
                "drop_rate_pct":  round(self.receiver.drop_rate_pct, 2),
                "n_rows":         result["n_rows"],
                "board_temp_c":   self.receiver.last_temp_c,
            }

            yield telemetry

            next_tick += self.interval
            sleep_time = next_tick - time.perf_counter()
            if sleep_time < -self.interval:
                log.warning(
                    "[Engine] Inference overrun: %.1f ms behind.",
                    -sleep_time * 1000,
                )
                next_tick = time.perf_counter()
                sleep_time = 0.0
            time.sleep(max(0.0, sleep_time))

def format_telemetry_json(telemetry: dict) -> str:
    """Sanitise and serialise telemetry to JSON."""
    try:
        return json.dumps(telemetry)
    except (ValueError, TypeError):
        # Fallback for non-finite floats
        sanitised = {
            k: (v if not (isinstance(v, float) and not math.isfinite(v)) else None)
            for k, v in telemetry.items()
        }
        return json.dumps(sanitised)
