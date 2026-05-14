# src/bridge_receiver.py
# Ingest provider for Arduino UNO Q via Bridge RPC (arduino-router IPC).
#
# The arduino-router daemon reads Bridge.notify("sensor_batch", bytes, 600)
# from the STM32U585 MCU over /dev/ttyHS1 and exposes batches via a named
# FIFO at BRIDGE_FIFO_PATH.  Each read from the FIFO returns exactly one
# batch: 25 × 24 = 600 bytes.
#
# Override the FIFO path with:
#   EDGEGUARD_BRIDGE_FIFO=/run/arduino/sensor_batch python main.py --mode bridge

import os
import time
import logging
import numpy as np
from typing import List, Tuple

from src.udp_receiver import BaseReceiver

log = logging.getLogger(__name__)

# Module-level default; actual value is resolved in __init__ so that
# setting EDGEGUARD_BRIDGE_FIFO after import still takes effect.
_DEFAULT_FIFO_PATH = "/run/arduino/sensor_batch"

# How long to wait for the FIFO to appear before giving up
FIFO_WAIT_TIMEOUT_S = 30.0


class BridgeParser:
    """Vectorised parser for 600-byte Bridge RPC batches (25 x 24-byte packets)."""

    DTYPE = np.dtype([
        ('timestamp_us', '<u4'),
        ('sequence_id',  '<u4'),
        ('accel_x',      '<f4'),
        ('accel_y',      '<f4'),
        ('accel_z',      '<f4'),
        ('board_temp',   '<f4'),
    ])
    PACKET_SIZE = 24  # bytes per SensorPayload struct
    BATCH_PACKETS = 25
    BATCH_SIZE = PACKET_SIZE * BATCH_PACKETS  # 600 bytes

    def parse_batch(self, data: bytes) -> List[Tuple[int, int, np.ndarray]]:
        """
        Vectorised parse — avoids per-row Python iteration.
        Returns list of (timestamp_us, sequence_id, features_np).
        features_np shape: (4,) float32  [accel_x, accel_y, accel_z, board_temp]
        """
        n = len(data) // self.PACKET_SIZE
        if n == 0:
            return []
        arr = np.frombuffer(data[:n * self.PACKET_SIZE], dtype=self.DTYPE)
        # Build (n, 4) feature matrix in one vectorised call — no Python loop
        feats = np.column_stack([
            arr['accel_x'].astype(np.float32),
            arr['accel_y'].astype(np.float32),
            arr['accel_z'].astype(np.float32),
            arr['board_temp'].astype(np.float32),
        ])
        return list(zip(
            arr['timestamp_us'].tolist(),
            arr['sequence_id'].tolist(),
            [feats[i] for i in range(n)],
        ))


class BridgeReceiver(BaseReceiver):
    """
    Ingest provider for UNO Q via arduino-router Bridge IPC FIFO.

    The arduino-router exposes Bridge.notify() payloads as a blocking
    named FIFO at fifo_path.  Each 600-byte read = one batch of 25
    sensor samples at 400 Hz.
    """

    def __init__(self, fifo_path: str | None = None):
        # FIX: resolve FIFO path at construction time, not at import time.
        # The previous module-level BRIDGE_FIFO_PATH = os.environ.get(...)
        # evaluated os.environ once when the module was imported, so setting
        # EDGEGUARD_BRIDGE_FIFO after import (e.g. in tests) had no effect.
        self.fifo_path = (
            fifo_path
            or os.environ.get("EDGEGUARD_BRIDGE_FIFO")
            or _DEFAULT_FIFO_PATH
        )
        self.parser = BridgeParser()
        self._last_seq: int | None = None
        self._last_temp_c: float | None = None
        self.total_received: int = 0
        self.total_dropped: int = 0

    def _read_exact(self, fifo, n: int, stop_event) -> bytes | None:
        """
        FIX: Read exactly n bytes from a named FIFO, handling short reads.

        Named FIFOs follow POSIX pipe semantics: a single read() call may
        return fewer bytes than requested even when more data is available
        (the writer may fill the pipe in multiple write() calls smaller than
        BATCH_SIZE, or the kernel pipe buffer may be partially full).

        The previous code used a bare fifo.read(BATCH_SIZE) and skipped
        with 'continue' on short reads, silently discarding every partial
        batch and creating invisible data gaps.

        This accumulator loop reads until exactly n bytes are collected.
        Returns None only on EOF (writer closed FIFO), so the caller can
        trigger a clean FIFO reopen.
        """
        buf = bytearray()
        while len(buf) < n:
            if stop_event.is_set():
                return None
            chunk = fifo.read(n - len(buf))
            if not chunk:
                # EOF: writer (arduino-router) closed its end of the FIFO
                return None
            buf.extend(chunk)
        return bytes(buf)

    def run(self, buf, stop_event) -> None:
        # ── Wait for arduino-router to create the FIFO ────────────────────────
        deadline = time.monotonic() + FIFO_WAIT_TIMEOUT_S
        while not os.path.exists(self.fifo_path):
            if stop_event.is_set():
                return
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"[BridgeReceiver] FIFO not found after "
                    f"{FIFO_WAIT_TIMEOUT_S}s: {self.fifo_path}. "
                    "Is arduino-router running? "
                    "Override path with EDGEGUARD_BRIDGE_FIFO env var."
                )
            time.sleep(0.1)

        log.info("[BridgeReceiver] Opening FIFO: %s", self.fifo_path)
        BATCH_SIZE = self.parser.BATCH_SIZE  # 600 bytes

        while not stop_event.is_set():
            try:
                with open(self.fifo_path, 'rb') as fifo:
                    log.info("[BridgeReceiver] FIFO open — ingesting batches.")
                    while not stop_event.is_set():
                        # FIX: use _read_exact() instead of fifo.read(BATCH_SIZE)
                        # to handle OS pipe short-reads transparently.
                        data = self._read_exact(fifo, BATCH_SIZE, stop_event)
                        if data is None:
                            # EOF: arduino-router closed its write end
                            log.warning(
                                "[BridgeReceiver] FIFO EOF — re-opening in 1s."
                            )
                            time.sleep(1.0)
                            break

                        samples = self.parser.parse_batch(data)
                        for _ts, seq, features in samples:
                            if self._last_seq is not None:
                                gap = (seq - self._last_seq - 1) & 0xFFFFFFFF
                                # Guard: large gaps indicate firmware reboot,
                                # not a real drop storm — reset rather than
                                # inflating drop_rate_pct by millions.
                                if gap > 10_000:
                                    log.warning(
                                        "[BridgeReceiver] Seq jump %d→%d "
                                        "(gap=%d): firmware reboot? Resetting.",
                                        self._last_seq, seq, gap,
                                    )
                                    gap = 0
                                self.total_dropped += int(gap)
                            self._last_seq = seq
                            self.total_received += 1
                            self._last_temp_c = round(float(features[3]), 2)
                            buf.add_row(features)

            except OSError as exc:
                if stop_event.is_set():
                    break
                log.error(
                    "[BridgeReceiver] FIFO error: %s — retrying in 2s.", exc
                )
                time.sleep(2.0)

        log.info(
            "[BridgeReceiver] Stopped. rx=%d dropped=%d",
            self.total_received, self.total_dropped,
        )

    @property
    def last_temp_c(self) -> float | None:
        return self._last_temp_c

    @property
    def drop_rate_pct(self) -> float:
        total = self.total_received + self.total_dropped
        if total == 0:
            return 0.0
        return 100.0 * self.total_dropped / total
