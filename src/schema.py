# src/schema.py
# Core data schema and constants for the EdgeGuard pipeline.

import os
from abc import ABC, abstractmethod

# ─── Domain Schema ────────────────────────────────────────────────────────
FEATURE_COLS = ["accel_x", "accel_y", "accel_z", "board_temp"]
N_FEATURES   = len(FEATURE_COLS)  # 4

# ─── Sampling Constants ───────────────────────────────────────────────────
SAMPLE_RATE_HZ = 400
WINDOW_SIZE    = 200
ROW_INTERVAL_MS = 1000.0 / SAMPLE_RATE_HZ

# ─── Hardware & Transport Configuration ────────────────────────────────────
BRIDGE_SOCK_PATH = "/var/run/arduino-router.sock"
BATCH_PACKETS = 100
PACKET_SIZE   = 24
BATCH_SIZE    = PACKET_SIZE * BATCH_PACKETS

# ─── Ingest Base Class ─────────────────────────────────────────────────────
class BaseReceiver(ABC):
    """Abstract base class for telemetry ingest providers."""

    @abstractmethod
    def run(self, buf, stop_event):
        """Main ingest loop."""
        pass

    @property
    @abstractmethod
    def last_temp_c(self) -> float:
        pass

    @property
    @abstractmethod
    def drop_rate_pct(self) -> float:
        pass
