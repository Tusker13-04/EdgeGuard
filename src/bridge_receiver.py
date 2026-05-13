import numpy as np
from typing import List, Tuple

class BridgeParser:
    # Structured dtype for 24-byte packet: timestamp(u4), sequence(u4), accel_x(f4), accel_y(f4), accel_z(f4), temp(f4)
    DTYPE = np.dtype([
        ('timestamp_us', '<u4'),
        ('sequence_id', '<u4'),
        ('accel_x', '<f4'),
        ('accel_y', '<f4'),
        ('accel_z', '<f4'),
        ('board_temp', '<f4'),
    ])
    PACKET_SIZE = 24

    def parse_batch(self, data: bytes) -> List[Tuple[int, int, np.ndarray]]:
        # Truncate trailing fragments
        num_packets = len(data) // self.PACKET_SIZE
        if num_packets == 0:
            return []
        
        # Vectorized unpacking
        truncated_data = data[:num_packets * self.PACKET_SIZE]
        structured_array = np.frombuffer(truncated_data, dtype=self.DTYPE)
        
        # Convert to requested output format: List[Tuple[ts, seq, np.array([ax, ay, az, temp])]]
        samples = []
        for row in structured_array:
            samples.append((
                int(row['timestamp_us']), 
                int(row['sequence_id']), 
                np.array([row['accel_x'], row['accel_y'], row['accel_z'], row['board_temp']], dtype=np.float32)
            ))
        return samples
