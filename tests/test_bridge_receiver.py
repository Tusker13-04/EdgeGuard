import pytest
import numpy as np
import struct
from src.bridge_receiver import BridgeParser
from src.schema import PACKET_SIZE

def test_parse_batch_bytes_count():
    parser = BridgeParser()
    # Mock a PACKET_SIZE * 2 batch
    raw_data = b'\x00' * (PACKET_SIZE * 2)
    results = parser.parse_batch_bytes(raw_data)
    assert len(results) == 2

def test_parse_batch_bytes_integrity():
    parser = BridgeParser()
    # Create a known payload: ts=100, seq=1, ax=1.1, ay=2.2, az=3.3, temp=4.4
    ts, seq = 100, 1
    features = [1.1, 2.2, 3.3, 4.4]
    packet = struct.pack('<LLffff', ts, seq, *features)
    
    results = parser.parse_batch_bytes(packet)
    assert len(results) == 1
    
    res_ts, res_seq, res_feats = results[0]
    assert res_ts == ts
    assert res_seq == seq
    np.testing.assert_allclose(res_feats, features, atol=1e-5)

def test_parse_batch_bytes_truncation():
    parser = BridgeParser()
    # 2 full packets + 5 trailing bytes
    raw_data = b'\x00' * (2 * PACKET_SIZE + 5)
    results = parser.parse_batch_bytes(raw_data)
    assert len(results) == 2

def test_parse_batch_bytes_empty():
    parser = BridgeParser()
    results = parser.parse_batch_bytes(b'')
    assert results == []

def test_parse_batch_bytes_non_finite():
    parser = BridgeParser()
    # Create a payload with NaN
    packet = struct.pack('<LLffff', 100, 1, 1.0, 1.0, 1.0, float('nan'))
    results = parser.parse_batch_bytes(packet)
    assert len(results) == 0  # Discarded
