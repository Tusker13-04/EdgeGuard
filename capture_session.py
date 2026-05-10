# capture_session.py
# Record a labelled data capture session for Edge Impulse training.
#
# Usage:
#   python capture_session.py --label normal   --duration 30
#   python capture_session.py --label imbalance --duration 30
#
# Output: data/raw/<label>/<timestamp>.csv  (Edge Impulse-ready)

import socket
import threading
import argparse

from src.buffer import FastCircularBuffer, DEFAULT_CAPACITY
from src.udp_receiver import PacketParser, PACKET_SIZE, N_FEATURES
from src.capture import record_session, SAMPLE_RATE_HZ

UDP_PORT = 4444
DATA_DIR = "data/raw"


def udp_ingest_thread(
    buf: FastCircularBuffer,
    parser: PacketParser,
    stop_event: threading.Event,
):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", UDP_PORT))
    sock.settimeout(1.0)
    print(f"[UDP] Listening on :{UDP_PORT}")
    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(64)
            result = parser.parse(data)
            if result is None:
                continue
            _ts, _seq, features, _jitter, _dropped = result
            buf.add_row(features)
        except socket.timeout:
            continue
    sock.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="EdgeGuard capture session")
    ap.add_argument(
        "--label", required=True, choices=["normal", "imbalance"],
        help="Fault class label for this recording",
    )
    ap.add_argument(
        "--duration", type=int, default=30,
        help="Recording duration in seconds (default: 30)",
    )
    ap.add_argument(
        "--out", default=DATA_DIR,
        help=f"Output directory (default: {DATA_DIR})",
    )
    args = ap.parse_args()

    buf        = FastCircularBuffer()          # DEFAULT_CAPACITY rows, N_FEATURES cols
    parser     = PacketParser()
    stop_event = threading.Event()

    ingest = threading.Thread(
        target=udp_ingest_thread,
        args=(buf, parser, stop_event),
        daemon=True,
    )
    ingest.start()

    saved = record_session(
        buffer=buf,
        label=args.label,
        output_dir=args.out,
        duration_seconds=args.duration,
    )

    stop_event.set()
    ingest.join(timeout=2.0)

    print()
    print("=" * 50)
    print(f"  Session complete: {args.label.upper()}")
    print(f"  Windows saved  : {len(saved)}")
    print(f"  Packets rx     : {parser.total_received}")
    print(f"  Packets dropped: {parser.total_dropped}")
    print(f"  Drop rate      : {parser.drop_rate_pct:.2f}%")
    print(f"  Expected rate  : {SAMPLE_RATE_HZ} Hz")
    print("=" * 50)
