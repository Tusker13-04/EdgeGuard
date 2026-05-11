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
import numpy as np

PACKET_FORMAT = '<LLffff'
PACKET_SIZE   = struct.calcsize(PACKET_FORMAT)  # 24 bytes
assert PACKET_SIZE == 24, f"Packet size mismatch: {PACKET_SIZE}"

# Feature column order written into the circular buffer
# Index: 0=accX, 1=accY, 2=accZ, 3=board_temp
FEATURE_COLS = ["accel_x", "accel_y", "accel_z", "board_temp"]
N_FEATURES   = len(FEATURE_COLS)  # 4


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
        if len(packet_bytes) != PACKET_SIZE:
            return None

        now = time.perf_counter()
        ts_us, seq_id, ax, ay, az, temp = struct.unpack(PACKET_FORMAT, packet_bytes)

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


def parse_payload(packet_bytes: bytes):
    """Lightweight stateless parse. Returns (timestamp_us, seq_id, features_np)."""
    if len(packet_bytes) != PACKET_SIZE:
        raise ValueError(f"Expected {PACKET_SIZE} bytes, got {len(packet_bytes)}")
    ts_us, seq_id, ax, ay, az, temp = struct.unpack(PACKET_FORMAT, packet_bytes)
    return ts_us, seq_id, np.array([ax, ay, az, temp], dtype=np.float32)
