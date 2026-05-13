# main.py
# EdgeGuard live inference pipeline — run on QRB2210 MPU (UNO Q) or Raspberry Pi.
#
# Architecture:
#   Thread 1 (ingest) : BridgeReceiver / UDPReceiver → FastCircularBuffer
#   Thread 2 (main)   : FastCircularBuffer → run_inference_cycle() → stdout
#
# Usage:
#   python main.py                            # live UDP mode (ESP8266)
#   python main.py --mode bridge              # live Bridge IPC mode (UNO Q)
#   python main.py --demo data/demo.jsonl     # replay a recorded telemetry file
#   python main.py --port 4444 --interval 0.5
#
# Environment variables:
#   EDGEGUARD_BRIDGE_FIFO  Override default Bridge IPC FIFO path
#
# Telemetry output (one JSON line per inference cycle, to stdout):
#   {"ts": ..., "label": "normal", "imbalance_prob": 0.02,
#    "board_temp_c": 27.4, "latency_ms": 4.1,
#    "drop_rate_pct": 0.0, "n_rows": 200}

import sys

# Python version guard: Union X | Y syntax and BridgeReceiver use 3.10+ features
if sys.version_info < (3, 10):
    raise RuntimeError(
        f"EdgeGuard requires Python >= 3.10. "
        f"Current: {sys.version_info.major}.{sys.version_info.minor}. "
        "Install Python 3.10+ or use 'python3.10 main.py'."
    )

import threading
import argparse
import time
import json
import logging
import signal
from typing import Union

from src.buffer import FastCircularBuffer
from src.udp_receiver import UDPReceiver
from src.bridge_receiver import BridgeReceiver
from src.inference import run_inference_cycle, load_model, INFERENCE_INTERVAL_S

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s %(name)s] %(message)s",
)
log = logging.getLogger("edgeguard")

UDP_PORT    = 4444
BRIDGE_PORT = 4445


# ---------------------------------------------------------------------------
# DEMO / REPLAY MODE
# ---------------------------------------------------------------------------

def run_demo(jsonl_path: str, interval: float) -> None:
    """
    Replay a pre-recorded .jsonl telemetry file in a loop.
    Each line is re-emitted to stdout at the original cadence so the
    dashboard behaves identically to live mode.
    If the file does not exist, a minimal synthetic sequence is generated.
    """
    import os
    import math

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

def run_live(receiver: Union["UDPReceiver", "BridgeReceiver"], interval: float) -> None:
    buf        = FastCircularBuffer()
    stop_event = threading.Event()
    sess       = load_model()

    def _shutdown(sig, frame):
        log.info("Shutting down…")
        stop_event.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Ingest thread with watchdog container
    _ingest_exc: list = [None]   # mutable container — no nonlocal needed

    def _ingest_guarded():
        try:
            receiver.run(buf, stop_event)
        except Exception as exc:
            _ingest_exc[0] = exc
            stop_event.set()   # signal main loop to shut down cleanly

    ingest = threading.Thread(target=_ingest_guarded, daemon=True)
    ingest.start()
    log.info("Inference loop starting at %.1fHz (mode: %s)",
             1.0 / interval, receiver.__class__.__name__)

    # Drift-resilient timer: advance anchor each cycle
    next_tick = time.perf_counter()

    while not stop_event.is_set():
        # Watchdog: surface ingest thread crash to operator
        if _ingest_exc[0] is not None:
            log.critical(
                "[Watchdog] Ingest thread died with: %s — shutting down.",
                _ingest_exc[0],
            )
            break

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

        # Drift-resilient sleep: advance tick anchor each cycle
        next_tick += interval
        sleep_time = next_tick - time.perf_counter()
        if sleep_time < -interval:
            # More than one full interval behind — reset anchor to avoid catch-up storm
            log.warning(
                "[Timing] Inference overrun: %.1f ms behind — resetting tick anchor.",
                -sleep_time * 1000,
            )
            next_tick = time.perf_counter()
            sleep_time = 0.0
        time.sleep(max(0.0, sleep_time))

    ingest.join(timeout=2.0)
    if _ingest_exc[0] is not None:
        raise RuntimeError(
            f"Pipeline terminated due to ingest thread failure: {_ingest_exc[0]}"
        ) from _ingest_exc[0]
    log.info("Pipeline stopped. Drop rate: %.2f%%", receiver.drop_rate_pct)


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="EdgeGuard live inference pipeline")
    ap.add_argument(
        "--mode", type=str, default="udp", choices=["udp", "bridge"],
        help="Ingest mode: 'udp' for ESP8266, 'bridge' for UNO Q Bridge IPC (default: udp)",
    )
    ap.add_argument(
        "--port", type=int, default=UDP_PORT,
        help=f"UDP port to listen on (UDP mode only, default: {UDP_PORT})",
    )
    ap.add_argument(
        "--bridge-fifo", type=str, default=None, metavar="PATH",
        help="Override Bridge IPC FIFO path (bridge mode only). "
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

    if args.demo is not None:
        run_demo(jsonl_path=args.demo, interval=args.interval)
    else:
        if args.mode == "udp":
            receiver: Union[UDPReceiver, BridgeReceiver] = UDPReceiver(port=args.port)
        else:
            import os
            fifo_path = (
                args.bridge_fifo
                or os.environ.get("EDGEGUARD_BRIDGE_FIFO")
                or "/run/arduino/sensor_batch"
            )
            receiver = BridgeReceiver(fifo_path=fifo_path)

        run_live(receiver=receiver, interval=args.interval)
