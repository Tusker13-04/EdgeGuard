import struct
import numpy as np
from src.udp_receiver import parse_payload, PACKET_FORMAT

def test_parse_payload():
    packet = struct.pack(PACKET_FORMAT, 1000000, 42, 1.1, 2.2, 3.3, 25.5)
    timestamp, seq_id, features = parse_payload(packet)
    assert timestamp == 1000000
    assert seq_id == 42
    assert len(features) == 4
    assert np.isclose(features[0], 1.1, atol=1e-5)
    assert np.isclose(features[3], 25.5, atol=1e-5)
