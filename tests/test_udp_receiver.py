import struct
import numpy as np
from src.udp_receiver import parse_payload

def test_parse_payload():
    packet = struct.pack("<LLfffff", 1000000, 42, 1.1, 2.2, 3.3, 25.5, 5.0)
    timestamp, seq_id, features = parse_payload(packet)
    assert timestamp == 1000000
    assert seq_id == 42
    assert len(features) == 5
    assert np.isclose(features[0], 1.1, atol=1e-5)
    assert np.isclose(features[4], 5.0, atol=1e-5)
