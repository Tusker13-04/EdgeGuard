# capture_session.py
# Record a labelled data capture session for Edge Impulse training.
# Target hardware: Arduino UNO Q (Qualcomm QRB2210 + STM32U585)
#
# Usage (UNO Q / Bridge IPC — production):
#   python capture_session.py --label normal    --duration 30
#   python capture_session.py --label imbalance --duration 30
#
# Usage (UDP — bench/dev testing without physical UNO Q hardware):
#   python capture_session.py --mode udp --label normal    --duration 30
#   python capture_session.py --mode udp --label imbalance --duration 30
#
# Override Bridge IPC FIFO path:
#   EDGEGUARD_BRIDGE_FIFO=/run/arduino/sensor_batch \
#     python capture_session.py --label normal
#
# Output: data/raw/<label>/<timestamp>_<index>.csv  (Edge Impulse-ready)

import os
import socket
import threading
import argparse

from src.buffer import FastCircularBuffer, DEFAULT_CAPACITY
from src.udp_receiver import PacketParser, PACKET_SIZE, N_FEATURES
from src.bridge_receiver import BridgeReceiver
from src.capture import record_session, SAMPLE_RATE_HZ

UDP_PORT = 4444
DATA_DIR = "data/raw"


def udp_ingest_thread(
    buf: FastCircularBuffer,
    parser: PacketParser,
    stop_event: threading.Event,
) -> None:
    """Legacy UDP ingest — bench/dev use only. Production uses BridgeReceiver."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", UDP_PORT))
    sock.settimeout(1.0)
    print(f"[UDP] Listening on :{UDP_PORT} (bench/dev mode)")
    while not stop_event.is_set():
        try:
            data, _addr = sock.recvfrom(64)
            result = parser.parse(data)
            if result is None:
                continue
            _ts, _seq, features, _jitter, _dropped = result
            buf.add_row(features)
        except socket.timeout:
            continue
    sock.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="EdgeGuard capture session (Arduino UNO Q)")
    ap.add_argument(
        "--mode", default="bridge", choices=["bridge", "udp"],
        help="Ingest mode: 'bridge' for UNO Q Bridge IPC (default), 'udp' for bench/dev only",
    )
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
    ap.add_argument(
        "--bridge-fifo", type=str, default=None, metavar="PATH",
        help="Override Bridge IPC FIFO path (bridge mode only).",
    )
    args = ap.parse_args()

    # FIX #2 (root cause): size the buffer to hold the full session.
    # DEFAULT_CAPACITY = 1600 rows = 4 s at 400 Hz.  A 30-second session needs
    # 12 000 rows.  Without this, get_snapshot() returns at most 1 600 rows and
    # data = snap[-new_rows_available:] silently clips to 1 600 even though
    # new_rows_available is 12 000, producing only ~8 windows instead of 60.
    required_capacity = args.duration * SAMPLE_RATE_HZ
    buf = FastCircularBuffer(capacity=max(DEFAULT_CAPACITY, required_capacity))

    stop_event = threading.Event()

    if args.mode == "udp":
        parser = PacketParser()
        ingest = threading.Thread(
            target=udp_ingest_thread,
            args=(buf, parser, stop_event),
            daemon=True,
        )
        ingest.start()
        stats_rx      = lambda: parser.total_received
        stats_dropped = lambda: parser.total_dropped
        stats_rate    = lambda: parser.drop_rate_pct
    else:
        fifo_path = (
            args.bridge_fifo
            or os.environ.get("EDGEGUARD_BRIDGE_FIFO")
            or "/run/arduino/sensor_batch"
        )
        print(f"[Bridge] Using FIFO: {fifo_path}")
        bridge = BridgeReceiver(socket_path=fifo_path)
        ingest = threading.Thread(
            target=bridge.run,
            args=(buf, stop_event),
            daemon=True,
        )
        ingest.start()
        stats_rx      = lambda: bridge.total_received
        stats_dropped = lambda: bridge.total_dropped
        stats_rate    = lambda: bridge.drop_rate_pct

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
    print(f"  Mode           : {args.mode.upper()}")
    print(f"  Windows saved  : {len(saved)}")
    print(f"  Packets rx     : {stats_rx()}")
    print(f"  Packets dropped: {stats_dropped()}")
    print(f"  Drop rate      : {stats_rate():.2f}%")
    print(f"  Expected rate  : {SAMPLE_RATE_HZ} Hz")
    print("=" * 50)
