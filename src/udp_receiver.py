import struct
import numpy as np

def parse_payload(packet_bytes: bytes):
    unpacked = struct.unpack('<LLfffff', packet_bytes)
    timestamp = unpacked[0]
    seq_id = unpacked[1]
    features = np.array(unpacked[2:7], dtype=np.float32)
    return timestamp, seq_id, features
