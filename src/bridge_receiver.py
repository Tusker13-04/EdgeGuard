# src/bridge_receiver.py
# Ingest provider for Arduino UNO Q via Bridge RPC (arduino-router IPC).
#
# The arduino-router daemon reads Bridge.notify("sensor_batch", bytes, 600)
# from the STM32U585 MCU over /dev/ttyHS1 and exposes batches via a named
# FIFO at BRIDGE_FIFO_PATH.  Each read from the FIFO returns exactly one
# batch: 25 x 24 = 600 bytes.
#
# Override the FIFO path with:
#   EDGEGUARD_BRIDGE_FIFO=/run/arduino/sensor_batch python main.py --mode bridge

import os
import stat
import select
import time
import logging
import threading
import numpy as np
from typing import List, Tuple

from src.udp_receiver import BaseReceiver

log = logging.getLogger(__name__)

_DEFAULT_FIFO_PATH = "/run/arduino/sensor_batch"

# How long to wait for the FIFO to appear before giving up
FIFO_WAIT_TIMEOUT_S = 30.0

# Physical sensor limits -- reject values outside these ranges
_ACCEL_LIMIT   = 200.0   # m/s^2  (+/-8g LIS3DH range = +/-78.4 m/s^2; 200 allows headroom)
_TEMP_MIN      = -40.0   # degC   DS18B20 rated minimum
_TEMP_MAX      = 125.0   # degC   DS18B20 rated maximum


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
    PACKET_SIZE   = 24   # bytes per SensorPayload struct
    BATCH_PACKETS = 25
    BATCH_SIZE    = PACKET_SIZE * BATCH_PACKETS  # 600 bytes

    def __init__(self):
        self._last_valid_temp: float | None = None

    def parse_batch(self, data: bytes) -> List[Tuple[int, int, np.ndarray]]:
        """
        Vectorised parse -- avoids per-row Python iteration.
        Returns list of (timestamp_us, sequence_id, features_np).
        features_np shape: (4,) float32  [accel_x, accel_y, accel_z, board_temp]

        FIX #10: validate all float fields for NaN/Inf before writing to
        the circular buffer.  np.frombuffer() silently produces NaN/Inf from
        malformed FIFO data; these propagate through ONNX and cause
        json.dumps() to raise ValueError, crashing the inference loop.
        Also sanitises the DS18B20 disconnect value (-127 degC) that the
        firmware can emit when the probe is missing (FIX #7).
        """
        n = len(data) // self.PACKET_SIZE
        if n == 0:
            return []
        arr = np.frombuffer(data[:n * self.PACKET_SIZE], dtype=self.DTYPE)

        feats = np.column_stack([
            arr['accel_x'].astype(np.float32),
            arr['accel_y'].astype(np.float32),
            arr['accel_z'].astype(np.float32),
            arr['board_temp'].astype(np.float32),
        ])

        # FIX #10: discard entire batch if any non-finite value is present
        if not np.all(np.isfinite(feats)):
            log.warning(
                "[BridgeParser] Non-finite (NaN/Inf) values in batch -- discarding %d samples.",
                n,
            )
            return []

        # FIX #10: clamp accelerometer columns to physical limits
        feats[:, :3] = np.clip(feats[:, :3], -_ACCEL_LIMIT, _ACCEL_LIMIT)

        # FIX #7: validate and substitute temperature column
        # DS18B20 returns -127.0 when the probe is disconnected; the firmware
        # guard (t > -100.0f) does not catch -127.0 exactly, so we must check
        # here too.  Substitute the last known good value (or 25 degC default).
        for i in range(n):
            t = float(feats[i, 3])
            if _TEMP_MIN <= t <= _TEMP_MAX:
                self._last_valid_temp = t
            else:
                feats[i, 3] = self._last_valid_temp if self._last_valid_temp is not None else 25.0

        return list(zip(
            arr['timestamp_us'].tolist(),
            arr['sequence_id'].tolist(),
            [feats[i] for i in range(n)],
        ))


class BridgeReceiver(BaseReceiver):
    """
    Ingest provider for UNO Q via arduino-router Bridge IPC FIFO.

    The arduino-router exposes Bridge.notify() payloads as a blocking named
    FIFO at fifo_path.  Each 600-byte read = one batch of 25 sensor samples
    at 400 Hz.
    """

    def __init__(self, fifo_path: str | None = None):
        # Resolve FIFO path at construction time so env-var overrides
        # set after module import (e.g. in tests) still take effect.
        self.fifo_path = (
            fifo_path
            or os.environ.get("EDGEGUARD_BRIDGE_FIFO")
            or _DEFAULT_FIFO_PATH
        )
        self.parser = BridgeParser()
        self._lock              = threading.Lock()  # FIX #14
        self._last_seq: int | None  = None
        self._last_temp_c: float | None = None
        # FIX (data race): _last_batch_time is read inside _lock (gap
        # plausibility) AND written here.  Initialise to monotonic clock so
        # the first batch's elapsed_s is a plausible small positive value.
        self._last_batch_time: float = time.monotonic()
        self.total_received: int = 0
        self.total_dropped:  int = 0

    # ---- FIX #4: TOCTOU-safe FIFO open -----------------------------------
    def _open_fifo_safe(self):
        """
        Open the FIFO with O_NONBLOCK to avoid hanging if the writer hasn't
        opened its end yet, then verify via fstat that the path is actually a
        FIFO (eliminates symlink-swap TOCTOU window), then switch back to
        blocking mode for normal operation.
        """
        fd = os.open(self.fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISFIFO(st.st_mode):
                os.close(fd)
                raise RuntimeError(
                    f"[BridgeReceiver] {self.fifo_path} is not a named FIFO "
                    f"(mode={oct(st.st_mode)}). Possible symlink attack or "
                    "misconfigured arduino-router."
                )
            # Switch to blocking mode after the fd has been validated
            os.set_blocking(fd, True)
        except Exception:
            os.close(fd)
            raise
        return os.fdopen(fd, 'rb')

    # ---- FIX #6: select-based _read_exact so stop_event is honoured ------
    def _read_exact(self, fifo, n: int, stop_event) -> bytes | None:
        """
        Read exactly n bytes from a named FIFO, handling short reads.

        Named FIFOs follow POSIX pipe semantics: a single read() call may
        return fewer bytes than requested (writer calls write() in smaller
        chunks, or the kernel pipe buffer is partially full).  The previous
        bare fifo.read(BATCH_SIZE) silently discarded every partial batch.

        FIX #6: use select() with a 500 ms timeout before each read so that
        stop_event is checked between blocked reads.  Without this, the thread
        can be blocked inside fifo.read() for up to FIFO_WAIT_TIMEOUT_S
        seconds after SIGINT, causing ingest.join(timeout=2.0) to time out
        and abandon the daemon thread.
        """
        buf = bytearray()
        while len(buf) < n:
            if stop_event.is_set():
                return None
            # Wait up to 500 ms for data to be available -- re-check stop_event
            ready, _, _ = select.select([fifo], [], [], 0.5)
            if not ready:
                continue  # timeout -- loop back to check stop_event
            chunk = fifo.read(n - len(buf))
            if not chunk:
                # EOF: arduino-router closed its write end
                return None
            buf.extend(chunk)
        return bytes(buf)

    def run(self, buf, stop_event) -> None:
        # ---- Wait for arduino-router to create the FIFO ------------------
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

        while not stop_event.is_set():
            try:
                # FIX #4: use TOCTOU-safe open that validates the path is a FIFO
                with self._open_fifo_safe() as fifo:
                    log.info("[BridgeReceiver] FIFO open -- ingesting batches.")
                    with self._lock:
                        self._last_batch_time = time.monotonic()
                    while not stop_event.is_set():
                        data = self._read_exact(fifo, self.parser.BATCH_SIZE, stop_event)
                        if data is None:
                            log.warning(
                                "[BridgeReceiver] FIFO EOF -- re-opening in 1s."
                            )
                            time.sleep(1.0)
                            break

                        now = time.monotonic()
                        samples = self.parser.parse_batch(data)
                        for _ts, seq, features in samples:
                            with self._lock:
                                if self._last_seq is not None:
                                    # FIX #5: wall-clock plausibility replaces
                                    # the fixed > 10_000 reboot heuristic.
                                    # At 400 Hz, gap samples take gap/400 s.
                                    # Any gap larger than 2x what the elapsed
                                    # time could produce is a firmware reboot.
                                    elapsed_s = now - self._last_batch_time
                                    max_plausible = max(
                                        int(elapsed_s * 400 * 2), 10_000
                                    )
                                    gap = (seq - self._last_seq - 1) & 0xFFFFFFFF
                                    if gap > max_plausible:
                                        log.warning(
                                            "[BridgeReceiver] Implausible seq gap "
                                            "%d->%d (gap=%d, elapsed=%.2fs, "
                                            "max_plausible=%d) -- treating as reboot.",
                                            self._last_seq, seq, gap,
                                            elapsed_s, max_plausible,
                                        )
                                        gap = 0
                                    self.total_dropped += int(gap)
                                self._last_seq = seq
                                self.total_received += 1
                                self._last_temp_c = round(float(features[3]), 2)
                                # FIX (data race): update _last_batch_time INSIDE
                                # _lock so the gap-plausibility read above is
                                # always consistent with the write here.  The
                                # previous code updated it OUTSIDE the lock,
                                # creating a TOCTOU window between the read
                                # (elapsed_s = now - self._last_batch_time inside
                                # the lock) and the write (outside the lock) that
                                # could produce a negative or zero elapsed_s and
                                # collapse max_plausible to 0, mis-classifying
                                # every valid batch as a firmware reboot.
                                self._last_batch_time = now
                            buf.add_row(features)

            except OSError as exc:
                if stop_event.is_set():
                    break
                log.error(
                    "[BridgeReceiver] FIFO error: %s -- retrying in 2s.", exc
                )
                time.sleep(2.0)

        log.info(
            "[BridgeReceiver] Stopped. rx=%d dropped=%d",
            self.total_received, self.total_dropped,
        )

    # FIX #14: guard cross-thread property reads with the same lock used
    # for write-side counters so total_received + total_dropped is consistent.
    @property
    def last_temp_c(self) -> float | None:
        with self._lock:
            return self._last_temp_c

    @property
    def drop_rate_pct(self) -> float:
        with self._lock:
            total = self.total_received + self.total_dropped
            if total == 0:
                return 0.0
            return 100.0 * self.total_dropped / total
