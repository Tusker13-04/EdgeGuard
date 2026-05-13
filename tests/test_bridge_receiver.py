import pytest
import numpy as np
import struct
from src.bridge_receiver import BridgeParser

def test_parse_batch_count():
    parser = BridgeParser()
    # Mock a 24-byte payload * 2 batch
    raw_data = b'\x00'*48 
    results = parser.parse_batch(raw_data)
    assert len(results) == 2

def test_parse_batch_integrity():
    parser = BridgeParser()
    # Create a known payload: ts=100, seq=1, ax=1.1, ay=2.2, az=3.3, temp=4.4
    ts, seq = 100, 1
    features = [1.1, 2.2, 3.3, 4.4]
    # <LLffff: uint32, uint32, float32, float32, float32, float32
    packet = struct.pack('<LLffff', ts, seq, *features)
    
    results = parser.parse_batch(packet)
    assert len(results) == 1
    
    res_ts, res_seq, res_feats = results[0]
    assert res_ts == ts
    assert res_seq == seq
    np.testing.assert_allclose(res_feats, features, atol=1e-5)

def test_parse_batch_truncation():
    parser = BridgeParser()
    # 2 full packets + 5 trailing bytes
    raw_data = b'\x00' * (2 * 24 + 5)
    results = parser.parse_batch(raw_data)
    assert len(results) == 2

def test_parse_batch_empty():
    parser = BridgeParser()
    results = parser.parse_batch(b'')
    assert results == []

def test_parse_batch_invalid_size():
    parser = BridgeParser()
    # Buffer smaller than one packet
    results = parser.parse_batch(b'\x00' * 10)
    assert results == []
