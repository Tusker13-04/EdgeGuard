# main.py
# EdgeGuard live inference pipeline — run on Raspberry Pi.
#
# Architecture:
#   Thread 1 (ingest) : UDP socket → PacketParser → FastCircularBuffer
#   Thread 2 (main)   : FastCircularBuffer → run_inference_cycle() → stdout
#
# Usage:
#   python main.py
#   python main.py --port 4444 --interval 0.5
#
# Telemetry output (one JSON line per inference cycle, to stdout):
#   {"ts": ..., "label": "normal", "imbalance_prob": 0.02,
#    "board_temp_c": 27.4, "latency_ms": 4.1,
#    "drop_rate_pct": 0.0, "n_rows": 1600}

import socket
import threading
import argparse
import time
import json
import logging
import signal
import sys

from src.buffer import FastCircularBuffer
from src.udp_receiver import PacketParser, PACKET_SIZE
from src.inference import run_inference_cycle, load_model, INFERENCE_INTERVAL_S

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s %(name)s] %(message)s",
)
log = logging.getLogger("edgeguard")

UDP_PORT = 4444


def udp_ingest_thread(
    buf: FastCircularBuffer,
    parser: PacketParser,
    stop_event: threading.Event,
    port: int,
):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", port))
    sock.settimeout(1.0)
    log.info("UDP ingest listening on :%d", port)
    while not stop_event.is_set():
        try:
            data, _ = sock.recvfrom(64)
            result = parser.parse(data)
            if result is None:
                continue
            _ts, _seq, features, _jitter, _dropped = result
            buf.add_row(features)
        except socket.timeout:
            continue
    sock.close()
    log.info("UDP ingest thread stopped.")


def run(port: int, interval: float):
    buf        = FastCircularBuffer()
    parser     = PacketParser()
    stop_event = threading.Event()
    sess       = load_model()  # None if no model file yet; fallback is automatic

    # Graceful shutdown on Ctrl+C or SIGTERM
    def _shutdown(sig, frame):
        log.info("Shutting down...")
        stop_event.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    ingest = threading.Thread(
        target=udp_ingest_thread,
        args=(buf, parser, stop_event, port),
        daemon=True,
    )
    ingest.start()
    log.info("Inference loop starting at %.1fHz", 1.0 / interval)

    while not stop_event.is_set():
        t0     = time.perf_counter()
        result = run_inference_cycle(buf, sess=sess)

        telemetry = {
            "ts":             round(time.time(), 3),
            "label":          result["label"],
            "imbalance_prob": result["imbalance_prob"],
            "normal_prob":    result["normal_prob"],
            "source":         result["source"],
            "latency_ms":     result["latency_ms"],
            "drop_rate_pct":  round(parser.drop_rate_pct, 2),
            "n_rows":         result["n_rows"],
            # board_temp_c: last temperature value seen from the DS18B20 sensor
            # (carried in UDP packet field board_temp, index 3 of features).
            # None until the first packet arrives; dashboard shows --- until then.
            "board_temp_c":   parser.last_temp_c,
        }
        print(json.dumps(telemetry), flush=True)

        # Sleep for remainder of interval
        elapsed = time.perf_counter() - t0
        sleep_s = max(0.0, interval - elapsed)
        time.sleep(sleep_s)

    ingest.join(timeout=2.0)
    log.info("Pipeline stopped. Total rx=%d dropped=%d (%.2f%%)",
             parser.total_received, parser.total_dropped, parser.drop_rate_pct)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="EdgeGuard live inference pipeline")
    ap.add_argument("--port",     type=int,   default=UDP_PORT,
                    help=f"UDP port to listen on (default: {UDP_PORT})")
    ap.add_argument("--interval", type=float, default=INFERENCE_INTERVAL_S,
                    help="Inference interval in seconds (default: 0.5)")
    args = ap.parse_args()
    run(port=args.port, interval=args.interval)
