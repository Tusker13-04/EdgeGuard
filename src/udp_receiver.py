import struct
import numpy as np

PACKET_FORMAT = '<LLffff'  # timestamp_us, seq_id, accel_x, accel_y, accel_z, temp
PACKET_SIZE = struct.calcsize(PACKET_FORMAT)  # 24 bytes

def parse_payload(packet_bytes: bytes):
    unpacked = struct.unpack(PACKET_FORMAT, packet_bytes)
    timestamp = unpacked[0]
    seq_id = unpacked[1]
    features = np.array(unpacked[2:6], dtype=np.float32)  # accX, accY, accZ, temp
    return timestamp, seq_id, features
