# src/udp_receiver.py
# Parses a single UDP packet from the ESP8266 LIS3DH firmware.
#
# SensorPayload layout (must match firmware/src/main.cpp):
#   uint32  timestamp_us   - microseconds since ESP8266 boot
#   uint32  sequence_id    - monotonic counter for drop detection
#   float32 accel_x        - m/s^2
#   float32 accel_y        - m/s^2
#   float32 accel_z        - m/s^2
#   float32 board_temp     - deg C (DS18B20 waterproof probe, ±0.5 °C)
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
        """Most recent board temperature in °C."""
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
        self._last_temp_c   = None   # most recent DS18B20 reading from firmware
        self.total_received = 0
        self.total_dropped  = 0

    def parse(self, packet_bytes: bytes):
        """
        Returns (timestamp_us, seq_id, features_np, jitter_ms, dropped_count)
        features_np shape: (4,) float32  [accX, accY, accZ, board_temp]
        Returns None if packet_bytes is wrong length.
        """
        # FIX: explicit length guard before unpack; prevents struct.error
        # on truncated/malformed datagrams from spoofed sources or network
        # fragmentation (UDP does not guarantee exact datagram sizes).
        if len(packet_bytes) != PACKET_SIZE:
            log.debug(
                "[PacketParser] Bad packet length: expected %d, got %d — dropped.",
                PACKET_SIZE, len(packet_bytes),
            )
            return None

        now = time.perf_counter()
        ts_us, seq_id, ax, ay, az, temp = struct.unpack(PACKET_FORMAT, packet_bytes)

        # Sanity-check decoded values before trusting them
        # Catches bit-flips, endianness mismatches, and firmware bugs
        if not (-200.0 <= ax <= 200.0 and -200.0 <= ay <= 200.0 and -200.0 <= az <= 200.0):
            log.warning(
                "[PacketParser] Implausible accel values (%.2f, %.2f, %.2f) seq=%d — dropped.",
                ax, ay, az, seq_id,
            )
            return None
        if not (-40.0 <= temp <= 125.0):
            log.warning(
                "[PacketParser] Implausible temperature %.2f°C seq=%d — clamping to last known.",
                temp, seq_id,
            )
            temp = self._last_temp_c if self._last_temp_c is not None else 25.0

        # Track latest temperature for telemetry broadcast
        self._last_temp_c = round(float(temp), 2)

        # Jitter
        jitter_ms = 0.0
        if self._last_arrival is not None:
            jitter_ms = (now - self._last_arrival) * 1000.0
        self._last_arrival = now

        # Drop detection
        dropped = 0
        if self._last_seq is not None:
            gap = (seq_id - self._last_seq - 1) & 0xFFFFFFFF
            # Guard: a gap > 10000 almost certainly means firmware reboot,
            # not a genuine drop storm. Reset counters to avoid poisoning stats.
            if gap > 10_000:
                log.warning(
                    "[PacketParser] Sequence jump %d → %d (gap=%d): "
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
        """Most recent board temperature in °C, or None before first packet."""
        return self._last_temp_c

    @property
    def drop_rate_pct(self) -> float:
        total = self.total_received + self.total_dropped
        if total == 0:
            return 0.0
        return 100.0 * self.total_dropped / total


class UDPReceiver(BaseReceiver):
    """UDP ingest provider for ESP8266 prototype."""

    def __init__(self, port: int = 4444):
        self.port = port
        self.parser = PacketParser()

    def run(self, buf, stop_event) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # FIX: set SO_REUSEADDR so the socket can be re-bound immediately
        # after a crash/restart without waiting for the OS TIME_WAIT period.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        sock.settimeout(1.0)
        log.info("[UDPReceiver] Listening on UDP :%d", self.port)

        # FIX: use try/finally to guarantee sock.close() even on exceptions.
        # Previously the socket was only closed at the bottom of the while loop,
        # so a KeyboardInterrupt or upstream exception leaked the OS file descriptor.
        try:
            while not stop_event.is_set():
                try:
                    # FIX: receive exactly PACKET_SIZE bytes.
                    # Previously recvfrom(64) accepted up to 64-byte datagrams;
                    # a 26-byte spoofed packet would be fed to parse() which
                    # returned None and silently incremented no counter,
                    # making it impossible to detect injection attempts.
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
            # Guaranteed cleanup — runs even on KeyboardInterrupt
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
