# dashboard/server.py
# EdgeGuard dashboard backend.
#
# Spawns main.py as a subprocess, reads its stdout (one JSON line per
# inference cycle), and broadcasts each message to all connected
# WebSocket clients.
#
# Usage:
#   uvicorn dashboard.server:app --host 0.0.0.0 --port 8080
#
# Environment variables:
#   EDGEGUARD_MODE            'bridge' (default) or 'udp'
#   EDGEGUARD_INFERENCE_MODE  'high_power' (default) or 'low_power'
#                             Sets the startup mode; overridden at runtime
#                             via POST /api/mode
#   EDGEGUARD_DEMO            Path to a .jsonl replay file (activates demo mode)
#   EDGEGUARD_BRIDGE_FIFO     Override Bridge IPC FIFO path
#   EDGEGUARD_MAX_CLIENTS     Max simultaneous WebSocket clients (default: 10)
#   EDGEGUARD_ALLOWED_ORIGINS Comma-separated allowed WS origins (empty = any)
#
# Inference mode API:
#   GET  /api/mode  -> { "mode": "high_power" | "low_power" }
#   POST /api/mode  body { "mode": "high_power" | "low_power" }
#                -> { "mode": "..." }  (echoes the new mode)
#
# Hot-swap mode change (Critique Fix 2):
#   set_mode() writes a JSON signal line to the subprocess stdin pipe.
#   main.py reads it in a daemon thread and sets/clears low_power_event.
#   No subprocess restart. No state loss. Mode change latency < 50ms.
#
# Autonomous MCU trigger (Critique Fix 1):
#   bridge_receiver.py fires POST /api/mode autonomously when the MCU
#   sends an anomaly_trigger RPC with imbalance_prob >= threshold.
#   This endpoint is the single source of truth for mode state.

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import sys
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

log = logging.getLogger("edgeguard.server")

app = FastAPI(title="EdgeGuard Dashboard")

MAIN_PY = str(Path(__file__).resolve().parent.parent / "main.py")

_DEMO_FILE       = os.environ.get("EDGEGUARD_DEMO",           "")
_MODE            = os.environ.get("EDGEGUARD_MODE",           "bridge")
_BRIDGE_FIFO     = os.environ.get("EDGEGUARD_BRIDGE_FIFO",    "")
try:
    _MAX_CLIENTS = int(os.environ.get("EDGEGUARD_MAX_CLIENTS", "10"))
except ValueError:
    _MAX_CLIENTS = 10

_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("EDGEGUARD_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS if _ALLOWED_ORIGINS else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Inference mode state ──────────────────────────────────────────────────
InferenceMode = Literal["high_power", "low_power"]
_inference_mode: InferenceMode = os.environ.get(  # type: ignore[assignment]
    "EDGEGUARD_INFERENCE_MODE", "high_power"
)


class ModeRequest(BaseModel):
    mode: InferenceMode


# ── Clients & queues ──────────────────────────────────────────────────────
_clients: set[WebSocket] = set()
_telemetry_queue: asyncio.Queue = asyncio.Queue(maxsize=50)
_background_tasks: set[asyncio.Task] = set()

# Subprocess stdin writer — set once the pipeline process is started
_stdin_writer: asyncio.StreamWriter | None = None
_stdin_lock: asyncio.Lock | None = None


# ── REST endpoints ────────────────────────────────────────────────────────

@app.get("/api/mode")
async def get_mode() -> JSONResponse:
    return JSONResponse({"mode": _inference_mode})


@app.post("/api/mode")
async def set_mode(req: ModeRequest) -> JSONResponse:
    """
    Update the active inference mode.

    Hot-swap path (Critique Fix 2):
      Writes a JSON signal line to the subprocess stdin pipe.
      main.py's stdin-reader thread picks it up within one poll cycle
      and sets/clears the low_power_event — no restart, no state loss.
    """
    global _inference_mode, _stdin_writer, _stdin_lock
    _inference_mode = req.mode
    log.info("[Server] Inference mode set to: %s", _inference_mode)

    if _stdin_lock is None:
        _stdin_lock = asyncio.Lock()

    # Signal the running subprocess via stdin — hot-swap, no restart
    async with _stdin_lock:
        if _stdin_writer is not None and not _stdin_writer.is_closing():
            try:
                signal_line = json.dumps({"mode": _inference_mode}) + "\n"
                _stdin_writer.write(signal_line.encode())
                await _stdin_writer.drain()
            except Exception as exc:
                log.warning("[Server] Failed to write mode signal to subprocess stdin: %s", exc)

    return JSONResponse({"mode": _inference_mode})


# ── Broadcast helpers ─────────────────────────────────────────────────────

async def _broadcast(message: str) -> None:
    """Send message to all clients concurrently with per-client timeout."""
    if not _clients:
        return
    dead: set[WebSocket] = set()
    sends = {
        ws: asyncio.create_task(
            asyncio.wait_for(ws.send_text(message), timeout=0.5)
        )
        for ws in list(_clients)
    }
    for ws, task in sends.items():
        try:
            await task
        except (asyncio.TimeoutError, Exception):
            dead.add(ws)
    _clients.difference_update(dead)


async def _broadcast_worker() -> None:
    """Consume telemetry from queue, ensure inference_mode is present, broadcast."""
    while True:
        try:
            raw = await _telemetry_queue.get()
            # inference_mode is now injected by engine.py; ensure it is present
            # as a fallback in case an older main.py build omits it.
            try:
                payload = json.loads(raw)
                if "inference_mode" not in payload:
                    payload["inference_mode"] = _inference_mode
                message = json.dumps(payload)
            except Exception:
                message = raw
            await _broadcast(message)
            _telemetry_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("[Server] Broadcast worker error: %s", exc)


# ── Pipeline reader ───────────────────────────────────────────────────────

async def _pipeline_reader() -> None:
    """
    Spawn main.py as a subprocess with stdin pipe open for hot-swap signals.

    stdout lines are validated JSON and enqueued for broadcast.
    stdin is kept open so set_mode() can write mode-change signals without
    restarting the process (Critique Fix 2).
    """
    global _stdin_writer

    cmd = [
        sys.executable, MAIN_PY,
        "--mode", _MODE,
        "--inference-mode", _inference_mode,
    ]
    if _DEMO_FILE:
        cmd += ["--demo", _DEMO_FILE]
    if _BRIDGE_FIFO and _MODE == "bridge":
        cmd += ["--bridge-fifo", _BRIDGE_FIFO]

    log.info("[Server] Spawning pipeline: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,   # open for hot-swap signals
        stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr,
    )
    assert proc.stdout is not None
    assert proc.stdin  is not None

    _stdin_writer = proc.stdin

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                log.warning("[Server] Pipeline subprocess exited.")
                break
            text = line.decode("utf-8").strip()
            if not text:
                continue
            try:
                json.loads(text)
                try:
                    _telemetry_queue.put_nowait(text)
                except asyncio.QueueFull:
                    log.warning("[Server] Telemetry queue full -- dropping frame.")
            except json.JSONDecodeError:
                pass
    finally:
        _stdin_writer = None
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def _pipeline_reader_with_restart() -> None:
    """Restart the pipeline on crash only (not on mode change)."""
    while True:
        try:
            await _pipeline_reader()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("[Server] Pipeline reader raised %s -- restarting.", exc)
        await asyncio.sleep(2.0)


# ── Startup ───────────────────────────────────────────────────────────────

_pipeline_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pipeline_task
    _pipeline_task = asyncio.create_task(_pipeline_reader_with_restart())
    task2 = asyncio.create_task(_broadcast_worker())

    for t in (_pipeline_task, task2):
        _background_tasks.add(t)
        t.add_done_callback(_background_tasks.discard)

    log.info(
        "[Server] Dashboard started. transport=%s inference_mode=%s max_clients=%d",
        _MODE, _inference_mode, _MAX_CLIENTS,
    )
    yield
    for t in list(_background_tasks):
        t.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)

app.router.lifespan_context = lifespan


# ── WebSocket & static ────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    if len(_clients) >= _MAX_CLIENTS:
        await ws.close(code=1008)
        return

    if _ALLOWED_ORIGINS:
        origin = ws.headers.get("origin", "")
        if origin not in _ALLOWED_ORIGINS:
            await ws.close(code=1008)
            return

    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = Path(__file__).parent / "edgeguard-dashboard.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/command-center", response_class=HTMLResponse)
async def command_center() -> HTMLResponse:
    """Serve the new EdgeGuard Command Center dashboard."""
    html_path = Path(__file__).parent / "edgeguard-command-center.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
