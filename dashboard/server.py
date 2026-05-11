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
# Demo mode (no hardware needed):
#   uvicorn dashboard.server:app --host 0.0.0.0 --port 8080
#   ...then set EDGEGUARD_DEMO env var or pass --demo flag to main.py:
#   EDGEGUARD_DEMO=data/demo.jsonl uvicorn dashboard.server:app ...
#
# Then open http://<pi-ip>:8080 in a browser.

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI(title="EdgeGuard Dashboard")

# Absolute path to main.py (one level up from this file)
MAIN_PY = str(Path(__file__).resolve().parent.parent / "main.py")

# If EDGEGUARD_DEMO env var is set, forward --demo flag to main.py subprocess
_DEMO_FILE = os.environ.get("EDGEGUARD_DEMO", "")

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

    stderr is forwarded to this process's stderr so crashes in main.py
    are visible in the uvicorn terminal instead of being silently dropped.
    """
    cmd = [sys.executable, MAIN_PY]
    if _DEMO_FILE:
        cmd += ["--demo", _DEMO_FILE]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr,          # forward — not devnull
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
