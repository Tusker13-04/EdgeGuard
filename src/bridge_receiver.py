import numpy as np
from typing import List, Tuple
import socket
from src.udp_receiver import BaseReceiver

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

class BridgeReceiver(BaseReceiver):
    """Ingest provider for UNO Q via Bridge RPC."""
    
    def __init__(self, port: int = 4445):
        self.port = port
        self.parser = BridgeParser()
        self._last_seq = None
        self._last_temp_c = None
        self.total_received = 0
        self.total_dropped = 0

    def run(self, buf, stop_event):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", self.port))
        sock.settimeout(1.0)
        
        while not stop_event.is_set():
            try:
                # Bridge RPC notifications can be larger than single packets (batches)
                data, _ = sock.recvfrom(4096)
                samples = self.parser.parse_batch(data)
                
                for ts, seq, features in samples:
                    # Update drop tracking
                    if self._last_seq is not None:
                        gap = (seq - self._last_seq - 1) & 0xFFFFFFFF
                        self.total_dropped += int(gap)
                    self._last_seq = seq
                    self.total_received += 1
                    
                    # Update last temp
                    self._last_temp_c = round(float(features[3]), 2)
                    
                    # Push to buffer
                    buf.add_row(features)
            except socket.timeout:
                continue
        sock.close()

    @property
    def last_temp_c(self):
        return self._last_temp_c

    @property
    def drop_rate_pct(self) -> float:
        total = self.total_received + self.total_dropped
        if total == 0:
            return 0.0
        return 100.0 * self.total_dropped / total
