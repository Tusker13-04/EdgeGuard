# main.py
# EdgeGuard live inference pipeline — runs on Arduino UNO Q (Qualcomm QRB2210 MPU).
#
# Architecture:
#   Ingest Thread  : BridgeReceiver/UDPReceiver → FastCircularBuffer
#   Main Thread    : PipelineEngine.run_inference_loop() → stdout
#
# Usage:
#   python main.py                            # Bridge IPC mode (UNO Q default)
#   python main.py --mode bridge              # explicit Bridge IPC mode
#   python main.py --mode udp                 # legacy UDP mode (bench/dev only)
#   python main.py --demo data/demo.jsonl     # replay a recorded telemetry file
#   python main.py --port 4444 --interval 0.5
#
# Telemetry output (one JSON line per inference cycle, to stdout):
#   {"ts": ..., "label": "normal", "imbalance_prob": 0.02,
#    "board_temp_c": 27.4, "latency_ms": 4.1,
#    "drop_rate_pct": 0.0, "n_rows": 200}

import sys

# Python version guard
if sys.version_info < (3, 10):
    raise RuntimeError(
        f"EdgeGuard requires Python >= 3.10. "
        f"Current: {sys.version_info.major}.{sys.version_info.minor}. "
        "Install Python 3.10+ or use 'python3.10 main.py'."
    )

import math
import threading
import argparse
import time
import json
import logging
import signal
from typing import Union

from src.udp_receiver import UDPReceiver
from src.bridge_receiver import BridgeReceiver
from src.engine import PipelineEngine, format_telemetry_json
from src.inference import INFERENCE_INTERVAL_S

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s %(name)s] %(message)s",
)
log = logging.getLogger("edgeguard")

UDP_PORT = 4444


# ---------------------------------------------------------------------------
# DEMO / REPLAY MODE
# ---------------------------------------------------------------------------

def run_demo(jsonl_path: str, interval: float) -> None:
    """
    Replay a pre-recorded .jsonl telemetry file in a loop.
    Each line is re-emitted to stdout at the original cadence so the
    dashboard behaves identically to live mode.
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
            rec["ts"] = round(time.time(), 3)
            print(json.dumps(rec), flush=True)
            time.sleep(interval)

    log.info("[Demo] Replay stopped.")


# ---------------------------------------------------------------------------
# LIVE MODE
# ---------------------------------------------------------------------------

def run_live(receiver: Union[UDPReceiver, BridgeReceiver], interval: float) -> None:
    engine = PipelineEngine(receiver=receiver, interval=interval)

    def _shutdown(sig, frame):
        engine.stop()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    engine.start()

    try:
        for telemetry in engine.run_inference_loop():
            print(format_telemetry_json(telemetry), flush=True)
    except Exception as exc:
        log.critical("[Main] Pipeline crashed: %s", exc)
        engine.stop()
        raise

    log.info("Pipeline stopped. Final drop rate: %.2f%%", receiver.drop_rate_pct)


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="EdgeGuard live inference pipeline (Arduino UNO Q)")
    ap.add_argument(
        "--mode", type=str, default="bridge", choices=["bridge", "udp"],
        help="Ingest mode: 'bridge' for UNO Q Bridge IPC (default), 'udp' for bench/dev testing",
    )
    ap.add_argument(
        "--port", type=int, default=UDP_PORT,
        help=f"UDP port (UDP bench mode only, default: {UDP_PORT})",
    )
    ap.add_argument(
        "--bridge-fifo", type=str, default=None, metavar="PATH",
        help="Override Bridge IPC FIFO path. "
             "Default: $EDGEGUARD_BRIDGE_FIFO or /run/arduino/sensor_batch",
    )
    ap.add_argument(
        "--interval", type=float, default=INFERENCE_INTERVAL_S,
        help=f"Inference interval in seconds (default: {INFERENCE_INTERVAL_S})",
    )
    ap.add_argument(
        "--demo", type=str, default=None, metavar="JSONL_FILE",
        help="Replay a recorded telemetry .jsonl file instead of live mode.",
    )
    args = ap.parse_args()

    # ISS-05: Reject non-positive interval before any I/O or thread start.
    if args.interval <= 0:
        ap.error("interval must be > 0 (got %s)" % args.interval)

    if args.demo is not None:
        run_demo(jsonl_path=args.demo, interval=args.interval)
    else:
        if args.mode == "udp":
            recv: Union[UDPReceiver, BridgeReceiver] = UDPReceiver(port=args.port)
        else:
            import os
            fifo_path = (
                args.bridge_fifo
                or os.environ.get("EDGEGUARD_BRIDGE_FIFO")
                or "/run/arduino/sensor_batch"
            )
            recv = BridgeReceiver(socket_path=fifo_path)

        run_live(receiver=recv, interval=args.interval)
