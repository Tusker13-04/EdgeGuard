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
#   EDGEGUARD_MODE            'udp' (default) or 'bridge'
#   EDGEGUARD_DEMO            Path to a .jsonl replay file (activates demo mode)
#   EDGEGUARD_BRIDGE_FIFO     Override Bridge IPC FIFO path
#   EDGEGUARD_MAX_CLIENTS     Max simultaneous WebSocket clients (default: 10)
#   EDGEGUARD_ALLOWED_ORIGINS Comma-separated allowed WS origins (empty = any)
#
# Then open http://<pi-ip>:8080 in a browser.

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

log = logging.getLogger("edgeguard.server")

app = FastAPI(title="EdgeGuard Dashboard")

# Absolute path to main.py (one level up from this file)
MAIN_PY = str(Path(__file__).resolve().parent.parent / "main.py")

_DEMO_FILE       = os.environ.get("EDGEGUARD_DEMO",    "")
_MODE            = os.environ.get("EDGEGUARD_MODE",    "udp")
_BRIDGE_FIFO     = os.environ.get("EDGEGUARD_BRIDGE_FIFO", "")
_MAX_CLIENTS     = int(os.environ.get("EDGEGUARD_MAX_CLIENTS", "10"))
_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("EDGEGUARD_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]

# Mutable set of live WebSocket clients
_clients: set[WebSocket] = set()

# Strong references to background tasks — prevents Python GC from cancelling them
_background_tasks: set[asyncio.Task] = set()


async def _broadcast(message: str) -> None:
    """
    Send message to all clients concurrently with a per-client send timeout.
    Dead/slow clients are removed from _clients.

    Uses list(_clients) snapshot before iterating to prevent RuntimeError
    if _clients is mutated by a concurrent websocket_endpoint coroutine.
    """
    dead: set[WebSocket] = set()
    sends = {
        ws: asyncio.create_task(
            asyncio.wait_for(ws.send_text(message), timeout=0.5)
        )
        for ws in list(_clients)   # snapshot to avoid set-changed-during-iteration
    }
    for ws, task in sends.items():
        try:
            await task
        except (asyncio.TimeoutError, Exception):
            dead.add(ws)
    _clients.difference_update(dead)


async def _pipeline_reader() -> None:
    """
    Spawn main.py, read its stdout line-by-line, broadcast each JSON
    telemetry line to all WebSocket clients.
    stderr is forwarded so crashes in main.py appear in the uvicorn terminal.
    """
    cmd = [sys.executable, MAIN_PY, "--mode", _MODE]
    if _DEMO_FILE:
        cmd += ["--demo", _DEMO_FILE]
    if _BRIDGE_FIFO and _MODE == "bridge":
        cmd += ["--bridge-fifo", _BRIDGE_FIFO]

    log.info("[Server] Spawning pipeline: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr,
    )
    assert proc.stdout is not None

    while True:
        line = await proc.stdout.readline()
        if not line:
            log.warning("[Server] Pipeline subprocess exited.")
            break
        text = line.decode("utf-8").strip()
        if not text:
            continue
        try:
            json.loads(text)   # validate JSON before broadcasting
            await _broadcast(text)
        except json.JSONDecodeError:
            pass

    # Ensure subprocess is fully cleaned up
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


async def _pipeline_reader_with_restart() -> None:
    """
    Wraps _pipeline_reader() with automatic restart on crash.
    Runs indefinitely until the ASGI server shuts down.
    """
    restart_delay = 2.0
    while True:
        try:
            await _pipeline_reader()
        except asyncio.CancelledError:
            log.info("[Server] Pipeline reader cancelled — shutting down.")
            break
        except Exception as exc:
            log.error(
                "[Server] Pipeline reader raised %s — restarting in %.1fs.",
                exc, restart_delay,
            )
        await asyncio.sleep(restart_delay)


@app.on_event("startup")
async def startup() -> None:
    # Store a strong reference so Python's GC does not cancel the task
    task = asyncio.create_task(_pipeline_reader_with_restart())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    log.info("[Server] Dashboard started. mode=%s max_clients=%d", _MODE, _MAX_CLIENTS)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    # Connection limit
    if len(_clients) >= _MAX_CLIENTS:
        await ws.close(code=1008)   # Policy Violation
        return

    # Optional origin allowlist
    if _ALLOWED_ORIGINS:
        origin = ws.headers.get("origin", "")
        if origin not in _ALLOWED_ORIGINS:
            await ws.close(code=1008)
            return

    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            await ws.receive_text()   # keep connection alive
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)         # always clean up on any disconnect


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
