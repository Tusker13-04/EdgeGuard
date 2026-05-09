# capture_session.py  (run from repo root)
import socket
import threading
import argparse
from src.buffer import FastCircularBuffer
from src.udp_receiver import parse_payload, PACKET_SIZE
from src.capture import record_session

UDP_PORT = 4444
BUFFER_CAPACITY = 4000
FEATURES = 4
DATA_DIR = "data/raw"

def udp_ingest_thread(buf: FastCircularBuffer, stop_event: threading.Event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", UDP_PORT))
    sock.settimeout(1.0)
    while not stop_event.is_set():
        try:
            data, _ = sock.recvfrom(64)
            _, _, features = parse_payload(data)
            buf.add_row(features)
        except socket.timeout:
            continue
    sock.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EdgeGuard capture session")
    parser.add_argument("--label", required=True, choices=["normal", "imbalance"],
                        help="Fault class label for this session")
    parser.add_argument("--duration", type=int, default=30,
                        help="Recording duration in seconds (default: 30)")
    args = parser.parse_args()

    buf = FastCircularBuffer(capacity=BUFFER_CAPACITY, features=FEATURES)
    stop_event = threading.Event()
    t = threading.Thread(target=udp_ingest_thread, args=(buf, stop_event), daemon=True)
    t.start()

    record_session(buf, label=args.label, output_dir=DATA_DIR, duration_seconds=args.duration)
    stop_event.set()
