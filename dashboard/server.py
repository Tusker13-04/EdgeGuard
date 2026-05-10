# dashboard/server.py
# EdgeGuard dashboard backend.
#
# Spawns main.py as a subprocess, reads its stdout (one JSON line per
# inference cycle), and broadcasts each message to all connected
# WebSocket clients.
#
# Usage:
#   uvicorn dashboard.server:app --host 0.0.0.0 --port 8080 --reload
#
# Then open http://<pi-ip>:8080 in a browser.

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI(title="EdgeGuard Dashboard")

# Absolute path to main.py (one level up from this file)
MAIN_PY = str(Path(__file__).resolve().parent.parent / "main.py")

# Connected WebSocket clients
_clients: set[WebSocket] = set()


async def _broadcast(message: str):
    dead = set()
    for ws in _clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)


async def _pipeline_reader():
    """
    Spawn main.py, read its stdout line-by-line, broadcast each JSON
    telemetry line to all WebSocket clients.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable, MAIN_PY,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8").strip()
        if not text:
            continue
        try:
            json.loads(text)   # validate before broadcasting
            await _broadcast(text)
        except json.JSONDecodeError:
            pass


@app.on_event("startup")
async def startup():
    asyncio.create_task(_pipeline_reader())


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            await ws.receive_text()   # keep connection alive
    except WebSocketDisconnect:
        _clients.discard(ws)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
