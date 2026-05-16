# dashboard/server.py
# EdgeGuard dashboard backend.
#
# Spawns main.py as a subprocess, reads its stdout (one JSON line per
# inference cycle), and broadcasts each message to all connected
# WebSocket clients.

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
_MODE            = os.environ.get("EDGEGUARD_MODE",    "bridge")
_BRIDGE_FIFO     = os.environ.get("EDGEGUARD_BRIDGE_FIFO", "")
_MAX_CLIENTS     = int(os.environ.get("EDGEGUARD_MAX_CLIENTS", "10"))
_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("EDGEGUARD_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]

# Mutable set of live WebSocket clients
_clients: set[WebSocket] = set()

# FIX FLAW-05: Decouple reader from broadcast with a bounded queue.
# Prevents slow WebSocket clients from creating back-pressure that stalls
# the main inference loop in main.py.
_telemetry_queue: asyncio.Queue = asyncio.Queue(maxsize=50)

# Strong references to background tasks -- prevents Python GC from cancelling them
_background_tasks: set[asyncio.Task] = set()


async def _broadcast(message: str) -> None:
    """
    Send message to all clients concurrently with a per-client send timeout.
    Dead/slow clients are removed from _clients.
    """
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
    """Consumes telemetry from the queue and broadcasts to all clients."""
    while True:
        try:
            message = await _telemetry_queue.get()
            await _broadcast(message)
            _telemetry_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("[Server] Broadcast worker error: %s", exc)


async def _pipeline_reader() -> None:
    """
    Spawn main.py, read its stdout line-by-line, broadcast each JSON
    telemetry line to all WebSocket clients.
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
            # FIX FLAW-05: Non-blocking put -- drop telemetry if dashboard is too slow
            try:
                _telemetry_queue.put_nowait(text)
            except asyncio.QueueFull:
                log.warning("[Server] Telemetry queue full -- dropping frame.")
        except json.JSONDecodeError:
            pass

    # Ensure subprocess is fully cleaned up
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


async def _pipeline_reader_with_restart() -> None:
    """Wraps _pipeline_reader() with automatic restart on crash."""
    restart_delay = 2.0
    while True:
        try:
            await _pipeline_reader()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("[Server] Pipeline reader raised %s -- restarting.", exc)
        await asyncio.sleep(restart_delay)


@app.on_event("startup")
async def startup() -> None:
    # Store strong references so Python's GC does not cancel the tasks
    task1 = asyncio.create_task(_pipeline_reader_with_restart())
    task2 = asyncio.create_task(_broadcast_worker())
    
    _background_tasks.add(task1)
    _background_tasks.add(task2)
    
    task1.add_done_callback(_background_tasks.discard)
    task2.add_done_callback(_background_tasks.discard)
    
    log.info("[Server] Dashboard started. mode=%s max_clients=%d", _MODE, _MAX_CLIENTS)


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
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
