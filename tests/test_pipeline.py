# tests/test_pipeline.py
# Full pipeline unit tests.
# Run with: pytest tests/test_pipeline.py -v

import struct
import numpy as np
import pytest

from src.udp_receiver import (
    parse_payload, PacketParser,
    PACKET_FORMAT, PACKET_SIZE, N_FEATURES, FEATURE_COLS,
)
from src.buffer import FastCircularBuffer, DEFAULT_CAPACITY
from src.capture import (
    slice_windows, save_window_as_csv,
    WINDOW_SIZE, SAMPLE_RATE_HZ, ROW_INTERVAL_MS, N_FEATURES as CAP_N_FEATURES,
)
from src.inference import run_inference_cycle


# ───────────────────────────────────────────────────────────────────────
# udp_receiver
# ───────────────────────────────────────────────────────────────────────

class TestUDPReceiver:

    def _make_packet(self, ts=1000, seq=0, ax=1.1, ay=2.2, az=9.8, temp=25.0):
        return struct.pack(PACKET_FORMAT, ts, seq, ax, ay, az, temp)

    def test_packet_size_is_24(self):
        assert PACKET_SIZE == 24

    def test_n_features_is_4(self):
        assert N_FEATURES == 4
        assert len(FEATURE_COLS) == 4

    def test_parse_payload_returns_correct_values(self):
        pkt = self._make_packet(ts=5000, seq=7, ax=1.5, ay=-2.0, az=9.81, temp=26.0)
        ts, seq, feats = parse_payload(pkt)
        assert ts == 5000
        assert seq == 7
        assert feats.shape == (4,)
        assert pytest.approx(feats[0], abs=1e-4) == 1.5
        assert pytest.approx(feats[1], abs=1e-4) == -2.0
        assert pytest.approx(feats[2], abs=1e-4) == 9.81
        assert pytest.approx(feats[3], abs=1e-4) == 26.0

    def test_parse_payload_wrong_size_raises(self):
        with pytest.raises(ValueError):
            parse_payload(b"short")

    def test_packet_parser_tracks_drops(self):
        parser = PacketParser()
        pkt0 = self._make_packet(ts=0, seq=0)
        pkt2 = self._make_packet(ts=5000, seq=2)  # seq 1 missing
        parser.parse(pkt0)
        parser.parse(pkt2)
        assert parser.total_dropped == 1
        assert parser.total_received == 2
        assert parser.drop_rate_pct == pytest.approx(100 * 1 / 3, abs=0.1)

    def test_packet_parser_no_drops(self):
        parser = PacketParser()
        for i in range(10):
            pkt = self._make_packet(seq=i)
            parser.parse(pkt)
        assert parser.total_dropped == 0
        assert parser.drop_rate_pct == 0.0


# ───────────────────────────────────────────────────────────────────────
# buffer
# ───────────────────────────────────────────────────────────────────────

class TestFastCircularBuffer:

    def _make_row(self, val):
        return np.array([val] * N_FEATURES, dtype=np.float32)

    def test_defaults(self):
        buf = FastCircularBuffer()
        assert buf.capacity == DEFAULT_CAPACITY
        assert buf.features == N_FEATURES

    def test_partial_fill_snapshot_is_chronological(self):
        buf = FastCircularBuffer(capacity=10)
        for i in range(4):
            buf.add_row(self._make_row(float(i)))
        snap = buf.get_snapshot()
        assert snap.shape == (4, N_FEATURES)
        assert snap[0, 0] == 0.0
        assert snap[3, 0] == 3.0

    def test_full_wrap_snapshot_is_chronological(self):
        buf = FastCircularBuffer(capacity=5)
        for i in range(7):  # overwrite by 2
            buf.add_row(self._make_row(float(i)))
        snap = buf.get_snapshot()
        assert snap.shape == (5, N_FEATURES)
        assert snap[0, 0] == 2.0   # oldest surviving row
        assert snap[-1, 0] == 6.0  # newest

    def test_n_rows_before_full(self):
        buf = FastCircularBuffer(capacity=10)
        for i in range(3):
            buf.add_row(self._make_row(float(i)))
        assert buf.n_rows == 3

    def test_n_rows_after_full(self):
        buf = FastCircularBuffer(capacity=5)
        for i in range(8):
            buf.add_row(self._make_row(float(i)))
        assert buf.n_rows == 5


# ───────────────────────────────────────────────────────────────────────
# capture
# ───────────────────────────────────────────────────────────────────────

class TestCapture:

    def test_constants_consistent(self):
        assert SAMPLE_RATE_HZ == 400
        assert WINDOW_SIZE == 200
        assert CAP_N_FEATURES == N_FEATURES
        assert pytest.approx(ROW_INTERVAL_MS) == 2.5

    def test_slice_windows_even(self):
        data = np.zeros((600, N_FEATURES), dtype=np.float32)
        windows = slice_windows(data, window_size=200)
        assert len(windows) == 3
        for w in windows:
            assert w.shape == (200, N_FEATURES)

    def test_slice_windows_discards_remainder(self):
        data = np.zeros((450, N_FEATURES), dtype=np.float32)
        windows = slice_windows(data, window_size=200)
        assert len(windows) == 2  # 50 rows discarded

    def test_save_window_csv_format(self, tmp_path):
        window = np.random.rand(WINDOW_SIZE, N_FEATURES).astype(np.float32)
        path = save_window_as_csv(window, label="normal", output_dir=str(tmp_path))
        import csv as csv_mod
        with open(path) as f:
            reader = list(csv_mod.reader(f))
        # Header
        assert reader[0] == ["timestamp"] + FEATURE_COLS
        # First row timestamp = 0.0
        assert float(reader[1][0]) == pytest.approx(0.0)
        # Second row timestamp = 2.5 ms
        assert float(reader[2][0]) == pytest.approx(2.5)
        # Row count = WINDOW_SIZE + 1 (header)
        assert len(reader) == WINDOW_SIZE + 1

    def test_save_window_csv_wrong_shape_raises(self, tmp_path):
        bad_window = np.zeros((100, N_FEATURES), dtype=np.float32)
        with pytest.raises(AssertionError):
            save_window_as_csv(bad_window, label="normal", output_dir=str(tmp_path))


# ───────────────────────────────────────────────────────────────────────
# inference
# ───────────────────────────────────────────────────────────────────────

class TestInference:

    def _make_buffer(self, n_rows, accel_rms_level=1.0):
        """Fill a buffer with synthetic data at a given vibration level."""
        buf = FastCircularBuffer(capacity=n_rows)
        for _ in range(n_rows):
            ax = np.random.normal(0, accel_rms_level)
            ay = np.random.normal(0, accel_rms_level)
            az = np.random.normal(9.8, accel_rms_level * 0.1)
            temp = 25.0
            buf.add_row(np.array([ax, ay, az, temp], dtype=np.float32))
        return buf

    def test_buffering_state_when_not_enough_rows(self):
        buf = FastCircularBuffer(capacity=500)
        for _ in range(10):  # fewer than WINDOW_SIZE
            buf.add_row(np.zeros(N_FEATURES, dtype=np.float32))
        result = run_inference_cycle(buf, sess=None)
        assert result["label"] == "buffering"

    def test_normal_classification(self):
        # Low vibration RMS → should be 'normal'
        buf = self._make_buffer(n_rows=400, accel_rms_level=1.0)
        result = run_inference_cycle(buf, sess=None)
        assert result["label"] == "normal"
        assert result["source"] == "rule_based"
        assert 0.0 <= result["imbalance_prob"] <= 1.0

    def test_anomaly_classification(self):
        # High vibration RMS → should be 'imbalance'
        buf = self._make_buffer(n_rows=400, accel_rms_level=20.0)
        result = run_inference_cycle(buf, sess=None)
        assert result["label"] == "imbalance"
        assert result["imbalance_prob"] > 0.5

    def test_result_has_required_keys(self):
        buf = self._make_buffer(n_rows=400)
        result = run_inference_cycle(buf, sess=None)
        for key in ("label", "imbalance_prob", "normal_prob", "source", "latency_ms", "n_rows"):
            assert key in result, f"Missing key: {key}"

    def test_latency_ms_is_non_negative(self):
        buf = self._make_buffer(n_rows=400)
        result = run_inference_cycle(buf, sess=None)
        assert result["latency_ms"] >= 0.0
