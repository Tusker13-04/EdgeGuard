# src/bridge_receiver.py
# Ingest provider for Arduino UNO Q via Bridge RPC Unix Domain Socket.
#
# Connects to the arduino-router daemon at /var/run/arduino-router.sock
# and listens for MessagePack notifications.
#
# RPC methods handled:
#   sensor_batch    — raw accelerometer batch (400 Hz, 100-sample chunks)
#   anomaly_trigger — MCU autonomous escalation: fires POST /api/mode=high_power
#                     when imbalance_prob >= EDGEGUARD_ANOMALY_TRIGGER_THRESHOLD

import os
import socket
import select
import time
import logging
import threading
import urllib.request
import urllib.error
import json
import ssl
import msgpack
import numpy as np
from typing import List, Tuple, Optional

from src.schema import BaseReceiver, BRIDGE_SOCK_PATH, PACKET_SIZE

log = logging.getLogger(__name__)

# Physical sensor limits
_ACCEL_LIMIT = 200.0
_TEMP_MIN    = -40.0
_TEMP_MAX    = 125.0

# ── Autonomous trigger config ─────────────────────────────────────────────
# imbalance_prob from MCU Edge Impulse classifier at or above this value
# causes bridge_receiver to escalate mode to high_power autonomously.
ANOMALY_TRIGGER_THRESHOLD: float = float(
    os.environ.get("EDGEGUARD_ANOMALY_TRIGGER_THRESHOLD", "0.75")
)
# After AUTO_COOLDOWN_S seconds with no anomaly_trigger events, revert to low_power.
AUTO_COOLDOWN_S: float = float(
    os.environ.get("EDGEGUARD_AUTO_COOLDOWN_S", "30.0")
)
# Dashboard server address (must match uvicorn bind)
_DASHBOARD_URL = os.environ.get("EDGEGUARD_DASHBOARD_URL", "http://127.0.0.1:8080")


class BridgeParser:
    """Vectorised parser for Bridge RPC MessagePack batches."""

    DTYPE = np.dtype([
        ('timestamp_us', '<u4'),
        ('sequence_id',  '<u4'),
        ('accel_x',      '<f4'),
        ('accel_y',      '<f4'),
        ('accel_z',      '<f4'),
        ('board_temp',   '<f4'),
    ])

    def __init__(self):
        # FIX FLAW-08: Initialise to 25.0 to avoid None at startup
        self._last_valid_temp: float = 25.0
        self._last_valid_accel = np.zeros(3, dtype=np.float32)

    def parse_batch_bytes(self, data: bytes) -> List[Tuple[int, int, np.ndarray]]:
        """Parses a raw byte block into a list of samples with per-sample NaN substitution."""
        n = len(data) // PACKET_SIZE
        if n == 0:
            return []

        arr = np.frombuffer(data[:n * PACKET_SIZE], dtype=self.DTYPE)
        feats = np.column_stack([
            arr['accel_x'].astype(np.float32),
            arr['accel_y'].astype(np.float32),
            arr['accel_z'].astype(np.float32),
            arr['board_temp'].astype(np.float32),
        ])

        # FIX FLAW-03: Per-sample substitution instead of whole-batch discard
        for i in range(n):
            if np.all(np.isfinite(feats[i, :3])):
                feats[i, :3] = np.clip(feats[i, :3], -_ACCEL_LIMIT, _ACCEL_LIMIT)
                self._last_valid_accel = feats[i, :3].copy()
            else:
                feats[i, :3] = self._last_valid_accel

            t = float(feats[i, 3])
            if np.isfinite(t) and _TEMP_MIN <= t <= _TEMP_MAX:
                self._last_valid_temp = t
            else:
                feats[i, 3] = self._last_valid_temp

        return list(zip(
            arr['timestamp_us'].tolist(),
            arr['sequence_id'].tolist(),
            [feats[i] for i in range(n)],
        ))


class BridgeReceiver(BaseReceiver):
    """
    High-performance ingest provider for UNO Q using Unix Sockets.

    Autonomous escalation path (Critique Fix 1):
      When the MCU sends an `anomaly_trigger` RPC notification whose
      imbalance_prob >= ANOMALY_TRIGGER_THRESHOLD, this receiver fires
      POST /api/mode {mode: high_power} to the dashboard server in a
      background thread — no human interaction required.
      After AUTO_COOLDOWN_S seconds with no further triggers, it
      automatically reverts to low_power.
    """

    def __init__(self, socket_path: str = BRIDGE_SOCK_PATH):
        self.socket_path = socket_path
        self.parser = BridgeParser()
        self._lock = threading.Lock()
        self._last_seq: Optional[int] = None
        self._last_temp_c: float = 25.0
        self._last_batch_time: float = time.monotonic()
        self.total_received: int = 0
        self.total_dropped:  int = 0

        # ── Autonomous trigger state ──────────────────────────────────────
        self._last_trigger_time: float = 0.0
        self._cooldown_thread: Optional[threading.Thread] = None
        self._cooldown_event: Optional[threading.Event] = None
        self._cooldown_lock = threading.Lock()
        
        # Thread Pool for background IO
        from concurrent.futures import ThreadPoolExecutor
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bridge-io")

    # ── Public run loop ───────────────────────────────────────────────────

    def run(self, buf, stop_event) -> None:
        while not stop_event.is_set():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(2.0)
                    log.info("[BridgeReceiver] Connecting to %s", self.socket_path)
                    sock.connect(self.socket_path)

                    unpacker = msgpack.Unpacker(raw=False)

                    while not stop_event.is_set():
                        ready, _, _ = select.select([sock], [], [], 0.5)
                        if not ready:
                            continue

                        chunk = sock.recv(4096)
                        if not chunk:
                            log.warning("[BridgeReceiver] Socket closed by router.")
                            break

                        unpacker.feed(chunk)
                        for msg in unpacker:
                            if not isinstance(msg, list) or len(msg) < 3:
                                continue

                            msg_type, method, params = msg[0], msg[1], msg[2]

                            if msg_type == 2 and method == "sensor_batch":
                                if isinstance(params, (list, tuple)) and len(params) > 0:
                                    self._handle_batch(params[0], buf)

                            elif msg_type == 2 and method == "anomaly_trigger":
                                # ── FIX CRITIQUE 1: MCU autonomous escalation ──
                                if isinstance(params, dict):
                                    self._handle_anomaly_trigger(params)
                                else:
                                    log.warning("[BridgeReceiver] anomaly_trigger params must be a dict")

            except (socket.error, ConnectionRefusedError) as exc:
                if not stop_event.is_set():
                    log.error("[BridgeReceiver] Connection error: %s. Retrying in 2s.", exc)
                    time.sleep(2.0)
            except Exception as exc:
                log.exception("[BridgeReceiver] Unexpected error: %s", exc)
                time.sleep(1.0)

    # ── Autonomous trigger handler ────────────────────────────────────────

    def _handle_anomaly_trigger(self, params: dict) -> None:
        """
        Called when the MCU sends an anomaly_trigger RPC notification.

        Expected params format (MessagePack dict):
          { "imbalance_prob": 0.91, "label": "imbalance" }

        If imbalance_prob >= ANOMALY_TRIGGER_THRESHOLD:
          - POST /api/mode {mode: high_power} to dashboard server
          - Start/reset a cooldown timer; after AUTO_COOLDOWN_S seconds
            with no further triggers, POST /api/mode {mode: low_power}
        """
        try:
            prob = float(params.get("imbalance_prob", 0.0))
        except (TypeError, ValueError):
            log.warning("[BridgeReceiver] anomaly_trigger: invalid params %r", params)
            return

        if prob < ANOMALY_TRIGGER_THRESHOLD:
            log.debug(
                "[BridgeReceiver] anomaly_trigger below threshold (%.3f < %.3f) — ignored.",
                prob, ANOMALY_TRIGGER_THRESHOLD,
            )
            return

        log.info(
            "[BridgeReceiver] MCU anomaly_trigger received (prob=%.3f >= %.3f) "
            "— escalating to high_power.",
            prob, ANOMALY_TRIGGER_THRESHOLD,
        )

        with self._cooldown_lock:
            now = time.monotonic()
            
            # Rate-limit escalate HTTP post to max 1 per 5s
            if not hasattr(self, "_last_post_time") or now - getattr(self, "_last_post_time", 0.0) > 5.0:
                self._last_post_time = now
                # Fire mode escalation in background thread to avoid blocking ingest
                self.executor.submit(self._post_mode, "high_power")

            self._last_trigger_time = now

        # Start/reset cooldown watcher
        self._reset_cooldown_thread()

    def _reset_cooldown_thread(self) -> None:
        """Start a new cooldown thread if one isn't already running."""
        with self._cooldown_lock:
            if self._cooldown_thread is None or not self._cooldown_thread.is_alive():
                if self._cooldown_event:
                    self._cooldown_event.set()
                self._cooldown_event = threading.Event()

                t = threading.Thread(
                    target=self._cooldown_worker,
                    args=(self._cooldown_event,),
                    daemon=True,
                    name="anomaly-cooldown",
                )
                self._cooldown_thread = t
                t.start()

    def _cooldown_worker(self, cancel_event: threading.Event) -> None:
        """
        Wait AUTO_COOLDOWN_S seconds from the last trigger time.
        If no new triggers arrive in that window, revert to low_power.
        """
        while not cancel_event.is_set():
            with self._cooldown_lock:
                deadline = self._last_trigger_time + AUTO_COOLDOWN_S

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if cancel_event.wait(timeout=min(remaining, 1.0)):
                return

        if not cancel_event.is_set():
            log.info(
                "[BridgeReceiver] Cooldown elapsed (%.0fs) — reverting to low_power.",
                AUTO_COOLDOWN_S,
            )
            self.executor.submit(self._post_mode, "low_power")

    @staticmethod
    def _post_mode(mode: str) -> None:
        """POST /api/mode to the dashboard server. Best-effort; logs on failure."""
        url  = f"{_DASHBOARD_URL}/api/mode"
        if not url.startswith(("http://", "https://")):
            log.error("[BridgeReceiver] Invalid dashboard URL scheme: %s", url)
            return

        body = json.dumps({"mode": mode}).encode()
        req  = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=2.0, context=ctx) as resp:
                log.info(
                    "[BridgeReceiver] Mode escalation -> %s acknowledged (HTTP %d).",
                    mode, resp.status,
                )
        except urllib.error.URLError as exc:
            log.warning(
                "[BridgeReceiver] Mode escalation -> %s failed: %s (dashboard unreachable?).",
                mode, exc,
            )

    # ── Batch handler ─────────────────────────────────────────────────────

    def _handle_batch(self, data: bytes, buf) -> None:
        now = time.monotonic()
        samples = self.parser.parse_batch_bytes(data)
        if not samples:
            return

        # FIX FLAW-04: Single lock acquisition per batch
        with self._lock:
            first_seq = samples[0][1]
            last_seq  = samples[-1][1]

            if self._last_seq is not None:
                elapsed_s = now - self._last_batch_time
                max_plausible = max(int(elapsed_s * 400 * 2), 10_000)
                gap = (first_seq - self._last_seq - 1) & 0xFFFFFFFF
                if gap > max_plausible:
                    log.warning("[BridgeReceiver] Implausible gap %d; treating as reboot.", gap)
                    gap = 0
                self.total_dropped += int(gap)

            internal_gap = (last_seq - first_seq + 1) - len(samples)
            if internal_gap > 0:
                self.total_dropped += int(internal_gap)

            self.total_received += len(samples)
            self._last_seq = last_seq
            self._last_batch_time = now
            self._last_temp_c = round(float(samples[-1][2][3]), 2)

        for _, _, features in samples:
            buf.add_row(features)

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def last_temp_c(self) -> float:
        with self._lock:
            return self._last_temp_c

    @property
    def drop_rate_pct(self) -> float:
        with self._lock:
            total = self.total_received + self.total_dropped
            if total == 0:
                return 0.0
            return 100.0 * self.total_dropped / total
