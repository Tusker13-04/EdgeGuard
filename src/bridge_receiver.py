# src/bridge_receiver.py
# Ingest provider for Arduino UNO Q via Bridge RPC Unix Domain Socket.
#
# Connects to the arduino-router daemon at /var/run/arduino-router.sock
# and listens for MessagePack notifications.

import os
import socket
import select
import time
import logging
import threading
import msgpack
import numpy as np
from typing import List, Tuple, Optional

from src.schema import BaseReceiver, BRIDGE_SOCK_PATH, BATCH_PACKETS, PACKET_SIZE

log = logging.getLogger(__name__)

# Physical sensor limits
_ACCEL_LIMIT = 200.0
_TEMP_MIN    = -40.0
_TEMP_MAX    = 125.0

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
            # 1. Accel validation
            if np.all(np.isfinite(feats[i, :3])):
                feats[i, :3] = np.clip(feats[i, :3], -_ACCEL_LIMIT, _ACCEL_LIMIT)
                self._last_valid_accel = feats[i, :3].copy()
            else:
                feats[i, :3] = self._last_valid_accel

            # 2. Temperature validation
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
    """

    def __init__(self, socket_path: str = BRIDGE_SOCK_PATH):
        self.socket_path = socket_path
        self.parser = BridgeParser()
        self._lock = threading.Lock()
        self._last_seq: Optional[int] = None
        self._last_temp_c: float = 25.0  # FIX FLAW-08: Default to 25.0
        self._last_batch_time: float = time.monotonic()
        self.total_received: int = 0
        self.total_dropped:  int = 0

    def run(self, buf, stop_event) -> None:
        while not stop_event.is_set():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(2.0)
                    log.info("[BridgeReceiver] Connecting to %s", self.socket_path)
                    sock.connect(self.socket_path)
                    
                    unpacker = msgpack.Unpacker(raw=False)
                    
                    while not stop_event.is_set():
                        # Use select to allow checking stop_event during idle
                        ready, _, _ = select.select([sock], [], [], 0.5)
                        if not ready:
                            continue
                            
                        chunk = sock.recv(4096)
                        if not chunk:
                            log.warning("[BridgeReceiver] Socket closed by router.")
                            break
                            
                        unpacker.feed(chunk)
                        for msg in unpacker:
                            # Bridge Notification format: [type=2, method, params]
                            if not isinstance(msg, list) or len(msg) < 3:
                                continue
                            
                            msg_type, method, params = msg[0], msg[1], msg[2]
                            if msg_type == 2 and method == "sensor_batch":
                                self._handle_batch(params[0], buf)

            except (socket.error, ConnectionRefusedError) as exc:
                if not stop_event.is_set():
                    log.error("[BridgeReceiver] Connection error: %s. Retrying in 2s.", exc)
                    time.sleep(2.0)
            except Exception as exc:
                log.exception("[BridgeReceiver] Unexpected error: %s", exc)
                time.sleep(1.0)

    def _handle_batch(self, data: bytes, buf) -> None:
        now = time.monotonic()
        samples = self.parser.parse_batch_bytes(data)
        if not samples:
            return

        # FIX FLAW-04: Single lock acquisition per batch for bulk counter updates
        with self._lock:
            first_seq = samples[0][1]
            last_seq  = samples[-1][1]
            
            if self._last_seq is not None:
                elapsed_s = now - self._last_batch_time
                max_plausible = max(int(elapsed_s * 400 * 2), 10_000)
                # Compute gap between last batch and this batch
                gap = (first_seq - self._last_seq - 1) & 0xFFFFFFFF
                if gap > max_plausible:
                    log.warning("[BridgeReceiver] Implausible gap %d; treating as reboot.", gap)
                    gap = 0
                self.total_dropped += int(gap)
            
            # Internal batch sequence integrity check
            internal_gap = (last_seq - first_seq + 1) - len(samples)
            if internal_gap > 0:
                self.total_dropped += int(internal_gap)

            self.total_received += len(samples)
            self._last_seq = last_seq
            self._last_batch_time = now
            self._last_temp_c = round(float(samples[-1][2][3]), 2)

        for _, _, features in samples:
            buf.add_row(features)

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
