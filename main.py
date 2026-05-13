# main.py
# EdgeGuard live inference pipeline — run on Raspberry Pi.
#
# Architecture:
#   Thread 1 (ingest) : UDP socket → PacketParser → FastCircularBuffer
#   Thread 2 (main)   : FastCircularBuffer → run_inference_cycle() → stdout
#
# Usage:
#   python main.py                          # live mode
#   python main.py --demo data/demo.jsonl   # replay a recorded telemetry file
#   python main.py --port 4444 --interval 0.5
#
# Demo mode:
#   Pass --demo <path-to-jsonl> to replay a pre-recorded telemetry log at
#   real-time speed (honouring the original inter-packet timestamps).
#   Each line must be a JSON object previously emitted by this script.
#   This guarantees the dashboard works end-to-end even without hardware.
#
# Telemetry output (one JSON line per inference cycle, to stdout):
#   {"ts": ..., "label": "normal", "imbalance_prob": 0.02,
#    "board_temp_c": 27.4, "latency_ms": 4.1,
#    "drop_rate_pct": 0.0, "n_rows": 200}

import socket
import threading
import argparse
import time
import json
import logging
import signal
import sys

from src.buffer import FastCircularBuffer
from src.udp_receiver import UDPReceiver
from src.bridge_receiver import BridgeReceiver
from src.inference import run_inference_cycle, load_model, INFERENCE_INTERVAL_S

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s %(name)s] %(message)s",
)
log = logging.getLogger("edgeguard")

UDP_PORT = 4444
BRIDGE_PORT = 4445


# ---------------------------------------------------------------------------
# DEMO / REPLAY MODE
# ---------------------------------------------------------------------------

def run_demo(jsonl_path: str, interval: float):
    """
    Replay a pre-recorded .jsonl telemetry file in a loop.
    Each line is re-emitted to stdout at the original cadence so the
    dashboard behaves identically to live mode.
    If the file does not exist, a minimal synthetic sequence is generated
    so the dashboard always has something to display.
    """
    import os

    def _load_lines(path):
        if not os.path.isfile(path):
            log.warning("[Demo] File not found: %s — generating synthetic sequence.", path)
            return _synthetic_lines()
        with open(path) as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            log.warning("[Demo] File is empty — generating synthetic sequence.")
            return _synthetic_lines()
        return lines

    def _synthetic_lines():
        """60 normal + 20 imbalance + 60 normal cycle for contrast demo."""
        import math
        lines = []
        for i in range(140):
            is_imbalance = 60 <= i < 80
            prob = 0.91 + 0.05 * math.sin(i) if is_imbalance else 0.03 + 0.01 * math.sin(i)
            temp = 38.5 + i * 0.05 if is_imbalance else 27.2 + 0.1 * math.sin(i * 0.3)
            rec = {
                "ts":             time.time(),
                "label":          "imbalance" if is_imbalance else "normal",
                "imbalance_prob": round(prob, 4),
                "normal_prob":    round(1.0 - prob, 4),
                "source":         "demo_synthetic",
                "latency_ms":     round(4.2 + math.sin(i) * 0.8, 2),
                "drop_rate_pct":  0.0,
                "n_rows":         200,
                "board_temp_c":   round(temp, 2),
            }
            lines.append(json.dumps(rec))
        return lines

    log.info("[Demo] Replay mode active. File: %s", jsonl_path)
    lines = _load_lines(jsonl_path)
    log.info("[Demo] Loaded %d telemetry frames. Looping at %.1f Hz.", len(lines), 1.0 / interval)

    stop_event = threading.Event()

    def _shutdown(sig, frame):
        stop_event.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while not stop_event.is_set():
        for raw in lines:
            if stop_event.is_set():
                break
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # Stamp with current wall-clock time so dashboard timestamps are live
            rec["ts"] = round(time.time(), 3)
            print(json.dumps(rec), flush=True)
            time.sleep(interval)

    log.info("[Demo] Replay stopped.")


# ---------------------------------------------------------------------------
# LIVE MODE
# ---------------------------------------------------------------------------

def run_live(receiver: UDPReceiver | BridgeReceiver, interval: float):
    buf        = FastCircularBuffer()
    stop_event = threading.Event()
    sess       = load_model()  # None if no model file yet; fallback is automatic

    def _shutdown(sig, frame):
        log.info("Shutting down...")
        stop_event.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    ingest = threading.Thread(
        target=receiver.run,
        args=(buf, stop_event),
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
            "drop_rate_pct":  round(receiver.drop_rate_pct, 2),
            "n_rows":         result["n_rows"],
            "board_temp_c":   receiver.last_temp_c,
        }
        print(json.dumps(telemetry), flush=True)

        elapsed = time.perf_counter() - t0
        time.sleep(max(0.0, interval - elapsed))

    ingest.join(timeout=2.0)
    log.info("Pipeline stopped. Drop rate: %.2f%%", receiver.drop_rate_pct)


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="EdgeGuard live inference pipeline")
    ap.add_argument("--mode",     type=str,   default="udp", choices=["udp", "bridge"],
                    help="Ingest mode: 'udp' for ESP8266, 'bridge' for UNO Q (default: udp)")
    ap.add_argument("--port",     type=int,   default=UDP_PORT,
                    help=f"Port to listen on (default: {UDP_PORT})")
    ap.add_argument("--interval", type=float, default=INFERENCE_INTERVAL_S,
                    help="Inference interval in seconds (default: 0.5)")
    ap.add_argument("--demo",     type=str,   default=None, metavar="JSONL_FILE",
                    help="Replay a recorded telemetry .jsonl file instead of live UDP. "
                         "If the file does not exist, a synthetic sequence is generated.")
    args = ap.parse_args()

    if args.demo is not None:
        run_demo(jsonl_path=args.demo, interval=args.interval)
    else:
        if args.mode == "udp":
            receiver = UDPReceiver(port=args.port)
        else:
            receiver = BridgeReceiver(port=args.port if args.port != UDP_PORT else BRIDGE_PORT)
        
        run_live(receiver=receiver, interval=args.interval)
