# src/udp_receiver.py
# Generic UDP packet parser — legacy transport used during early prototyping.
# On the production Arduino UNO Q target, BridgeReceiver (bridge_receiver.py)
# is the active ingest path. UDPReceiver is retained only for bench testing
# and local development without physical UNO Q hardware.
#
# SensorPayload layout (must match firmware/uno_q_main/uno_q_main.ino):
#   uint32  timestamp_us   - microseconds since MCU boot
#   uint32  sequence_id    - monotonic counter for drop detection
#   float32 accel_x        - m/s^2
#   float32 accel_y        - m/s^2
#   float32 accel_z        - m/s^2
#   float32 board_temp     - deg C (DS18B20 waterproof probe, +/-0.5 degC)
#
# Total: 24 bytes

import struct
import time
import logging
import numpy as np
from abc import ABC, abstractmethod
import socket

log = logging.getLogger(__name__)

PACKET_FORMAT = '<LLffff'
PACKET_SIZE   = struct.calcsize(PACKET_FORMAT)  # 24 bytes
assert PACKET_SIZE == 24, f"Packet size mismatch: {PACKET_SIZE}"

# Feature column order written into the circular buffer
# Index: 0=accX, 1=accY, 2=accZ, 3=board_temp
FEATURE_COLS = ["accel_x", "accel_y", "accel_z", "board_temp"]
N_FEATURES   = len(FEATURE_COLS)  # 4


class BaseReceiver(ABC):
    """Abstract base class for telemetry ingest providers."""

    @abstractmethod
    def run(self, buf, stop_event):
        """Main ingest loop. Should block until stop_event is set."""
        pass

    @property
    @abstractmethod
    def last_temp_c(self):
        """Most recent board temperature in degrees C."""
        pass

    @property
    @abstractmethod
    def drop_rate_pct(self) -> float:
        """Current packet drop rate as a percentage."""
        pass


class PacketParser:
    """Stateful parser that tracks sequence gaps, inter-arrival jitter,
    and the most recently received board temperature."""

    def __init__(self):
        self._last_seq      = None
        self._last_arrival  = None
        self._last_temp_c   = None
        self.total_received = 0
        self.total_dropped  = 0

    def parse(self, packet_bytes: bytes):
        """
        Returns (timestamp_us, seq_id, features_np, jitter_ms, dropped_count)
        features_np shape: (4,) float32  [accX, accY, accZ, board_temp]
        Returns None if packet_bytes is wrong length.
        """
        if len(packet_bytes) != PACKET_SIZE:
            log.debug(
                "[PacketParser] Bad packet length: expected %d, got %d — dropped.",
                PACKET_SIZE, len(packet_bytes),
            )
            return None

        now = time.perf_counter()
        ts_us, seq_id, ax, ay, az, temp = struct.unpack(PACKET_FORMAT, packet_bytes)

        if not (-200.0 <= ax <= 200.0 and -200.0 <= ay <= 200.0 and -200.0 <= az <= 200.0):
            log.warning(
                "[PacketParser] Implausible accel values (%.2f, %.2f, %.2f) seq=%d — dropped.",
                ax, ay, az, seq_id,
            )
            return None
        if not (-40.0 <= temp <= 125.0):
            log.warning(
                "[PacketParser] Implausible temperature %.2f deg C seq=%d — clamping to last known.",
                temp, seq_id,
            )
            temp = self._last_temp_c if self._last_temp_c is not None else 25.0

        self._last_temp_c = round(float(temp), 2)

        jitter_ms = 0.0
        if self._last_arrival is not None:
            jitter_ms = (now - self._last_arrival) * 1000.0
        self._last_arrival = now

        dropped = 0
        if self._last_seq is not None:
            gap = (seq_id - self._last_seq - 1) & 0xFFFFFFFF
            if gap > 10_000:
                log.warning(
                    "[PacketParser] Sequence jump %d -> %d (gap=%d): "
                    "firmware likely rebooted. Resetting drop counter.",
                    self._last_seq, seq_id, gap,
                )
                gap = 0
            dropped = int(gap)
            self.total_dropped += dropped
        self._last_seq = seq_id
        self.total_received += 1

        features = np.array([ax, ay, az, temp], dtype=np.float32)
        return ts_us, seq_id, features, jitter_ms, dropped

    @property
    def last_temp_c(self):
        """Most recent board temperature in degrees C, or None before first packet."""
        return self._last_temp_c

    @property
    def drop_rate_pct(self) -> float:
        total = self.total_received + self.total_dropped
        if total == 0:
            return 0.0
        return 100.0 * self.total_dropped / total


class UDPReceiver(BaseReceiver):
    """
    UDP ingest provider — legacy transport for bench testing without UNO Q hardware.
    Production ingest on Arduino UNO Q uses BridgeReceiver (bridge_receiver.py).

    FIX #6 (UDP injection): binds to 127.0.0.1 by default so only processes
    on the same host can send packets.  Pass bind_host="" or bind_host="0.0.0.0"
    explicitly only when LAN access (e.g. physical ESP8266) is required.
    """

    def __init__(self, port: int = 4444, bind_host: str = "127.0.0.1"):
        self.port      = port
        self.bind_host = bind_host
        self.parser    = PacketParser()

    def run(self, buf, stop_event) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind_host, self.port))
        sock.settimeout(1.0)
        log.info("[UDPReceiver] Listening on UDP %s:%d", self.bind_host or "0.0.0.0", self.port)
        try:
            while not stop_event.is_set():
                try:
                    data, _addr = sock.recvfrom(PACKET_SIZE)
                    result = self.parser.parse(data)
                    if result is None:
                        continue
                    _ts, _seq, features, _jitter, _dropped = result
                    buf.add_row(features)
                except socket.timeout:
                    continue
                except OSError as exc:
                    if stop_event.is_set():
                        break
                    log.error("[UDPReceiver] socket error: %s", exc)
                    time.sleep(0.5)
        finally:
            sock.close()
            log.info(
                "[UDPReceiver] Stopped. rx=%d dropped=%d drop_rate=%.2f%%",
                self.parser.total_received,
                self.parser.total_dropped,
                self.parser.drop_rate_pct,
            )

    @property
    def last_temp_c(self):
        return self.parser.last_temp_c

    @property
    def drop_rate_pct(self) -> float:
        return self.parser.drop_rate_pct


def parse_payload(packet_bytes: bytes):
    """Lightweight stateless parse. Returns (timestamp_us, seq_id, features_np)."""
    if len(packet_bytes) != PACKET_SIZE:
        raise ValueError(f"Expected {PACKET_SIZE} bytes, got {len(packet_bytes)}")
    ts_us, seq_id, ax, ay, az, temp = struct.unpack(PACKET_FORMAT, packet_bytes)
    return ts_us, seq_id, np.array([ax, ay, az, temp], dtype=np.float32)
