# main.py
# EdgeGuard live inference pipeline — runs on Arduino UNO Q (Qualcomm QRB2210 MPU).
#
# Architecture:
#   Ingest Thread  : BridgeReceiver/UDPReceiver → FastCircularBuffer
#   Stdin Thread   : reads {"mode": ...} signal lines from server.py (hot-swap)
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
#    "drop_rate_pct": 0.0, "n_rows": 200,
#    "inference_mode": "high_power" | "low_power"}
#
# Hot-swap mode signal (Critique Fix 2):
#   server.py writes a single JSON line to this process's stdin:
#     {"mode": "low_power"}   → sets   low_power_event (skips ONNX next tick)
#     {"mode": "high_power"}  → clears low_power_event (resumes ONNX next tick)
#   No process restart. Mode change latency < 50ms.

import sys

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
import os
from typing import Union

from src.udp_receiver import UDPReceiver
from src.bridge_receiver import BridgeReceiver
from src.engine import EdgeGuardEngine
from src.buffer import FastCircularBuffer
from src.inference import INFERENCE_INTERVAL_S

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s %(name)s] %(message)s",
)
log = logging.getLogger("edgeguard")

UDP_PORT = 4444


# ---------------------------------------------------------------------------
# STDIN MODE SIGNAL READER (hot-swap, Critique Fix 2)
# ---------------------------------------------------------------------------

def _start_stdin_mode_reader(low_power_event: threading.Event) -> threading.Thread:
    """
    Daemon thread that reads JSON signal lines from stdin.
    server.py writes {"mode": "low_power"} or {"mode": "high_power"}.
    Sets/clears low_power_event accordingly.
    Exits silently when stdin closes (subprocess lifetime).
    """
    def _reader():
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            mode = msg.get("mode")
            if mode == "low_power":
                low_power_event.set()
                log.info("[StdinReader] Mode signal received: low_power")
            elif mode == "high_power":
                low_power_event.clear()
                log.info("[StdinReader] Mode signal received: high_power")
            else:
                log.debug("[StdinReader] Unknown mode signal: %r", mode)

    t = threading.Thread(target=_reader, daemon=True, name="stdin-mode-reader")
    t.start()
    return t


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
                "inference_mode": "high_power",
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

    next_tick = time.perf_counter()
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
            
            next_tick += interval
            sleep_time = next_tick - time.perf_counter()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                next_tick = time.perf_counter()

    log.info("[Demo] Replay stopped.")


# ---------------------------------------------------------------------------
# LIVE MODE
# ---------------------------------------------------------------------------

def run_live(
    receiver: Union[UDPReceiver, BridgeReceiver],
    interval: float,
    initial_mode: str = "high_power",
) -> None:
    """
    Start the live inference pipeline.
    """
    low_power_event = threading.Event()
    if initial_mode == "low_power":
        low_power_event.set()

    _start_stdin_mode_reader(low_power_event)

    buf = FastCircularBuffer()
    stop_event = threading.Event()

    # Stub put() for UDP bench receiver which lacks it
    if not hasattr(receiver, "put"):
        receiver.put = lambda cmd, payload: None

    engine = EdgeGuardEngine(bridge=receiver)

    def _shutdown(sig, frame):
        stop_event.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    ingest_thread = threading.Thread(
        target=receiver.run, args=(buf, stop_event), daemon=True
    )
    ingest_thread.start()

    try:
        while not stop_event.is_set():
            time.sleep(interval)
            is_low_power = low_power_event.is_set()
            receiver_temp = getattr(receiver, "last_temp_c", 25.0)
            board_temp_c = 25.0 if receiver_temp is None else receiver_temp
            
            if is_low_power:
                # Emit keepalive frame during low_power so dashboard stays connected
                keepalive = {
                    "ts":             time.time(),
                    "source":         "keepalive",
                    "label":          "nominal",
                    "diagnostic":     "Low Power Mode — Cognition Paused",
                    "imbalance_prob": 0.0,
                    "confidence":     1.0,
                    "temp_state":     "normal",
                    "board_temp_c":   board_temp_c,
                    "raw_probs":      {"normal": 1.0, "imbalance": 0.0, "bearing": 0.0, "looseness": 0.0},
                    "latency_ms":     0.0,
                    "drop_rate_pct":  getattr(receiver, "drop_rate_pct", 0.0),
                    "n_rows":         0,
                    "inference_mode": "low_power"
                }
                print(json.dumps(keepalive), flush=True)
                continue
                
            snap = buf.get_snapshot()
            if len(snap) == 0:
                continue
                
            import numpy as np
            accel_data = np.array(snap).reshape(-1, 4)[:, :3]
            variance = np.var(accel_data)
            
            if is_low_power:
                if variance > 0.05:
                    log.info("Vibration detected, waking up from low_power mode.")
                    low_power_event.clear()
                    if hasattr(receiver, "idle_start"):
                        del receiver.idle_start
            else:
                if variance < 0.01:
                    if not hasattr(receiver, "idle_start"):
                        receiver.idle_start = time.time()
                    elif time.time() - receiver.idle_start > 300:
                        log.info("No vibration for 5 minutes, engaging adaptive low_power mode.")
                        low_power_event.set()
                else:
                    if hasattr(receiver, "idle_start"):
                        del receiver.idle_start
                
            t0 = time.perf_counter()
            telemetry = engine.process_batch(
                snap.flatten().tolist(), 
                board_temp_c=board_temp_c,
                source="sensor_batch"
            )
            telemetry["latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
            telemetry["drop_rate_pct"] = getattr(receiver, "drop_rate_pct", 0.0)
            telemetry["n_rows"] = len(snap)
            telemetry["inference_mode"] = "high_power"
            
            print(json.dumps(telemetry), flush=True)
            
    except Exception as exc:
        log.critical("[Main] Pipeline crashed: %s", exc, exc_info=True)
        stop_event.set()
        raise
    finally:
        if ingest_thread.is_alive():
            ingest_thread.join(timeout=1.0)

    log.info("Pipeline stopped. Final drop rate: %.2f%%", getattr(receiver, "drop_rate_pct", 0.0))


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def get_bridge_fifo_path(override: str | None) -> str:
    if override:
        return override
    return os.environ.get("EDGEGUARD_BRIDGE_FIFO", "/run/arduino/sensor_batch")


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
    ap.add_argument(
        "--inference-mode", type=str, default="high_power",
        choices=["high_power", "low_power"],
        help="Initial inference mode (default: high_power). "
             "Overridden at runtime via stdin signals from server.py.",
    )
    args = ap.parse_args()

    if args.interval <= 0:
        ap.error("interval must be > 0 (got %s)" % args.interval)

    if args.demo is not None:
        run_demo(jsonl_path=args.demo, interval=args.interval)
    else:
        if args.mode == "udp":
            recv: Union[UDPReceiver, BridgeReceiver] = UDPReceiver(port=args.port)
        else:
            fifo_path = get_bridge_fifo_path(args.bridge_fifo)
            recv = BridgeReceiver(socket_path=fifo_path)

        run_live(receiver=recv, interval=args.interval, initial_mode=args.inference_mode)
