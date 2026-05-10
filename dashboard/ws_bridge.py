# dashboard/ws_bridge.py
# FastAPI WebSocket bridge: runs main.py as a subprocess and
# forwards each JSON telemetry line to all connected browser clients.
#
# Usage (from repo root):
#   uvicorn dashboard.ws_bridge:app --host 0.0.0.0 --port 8000
#
# Then open dashboard/index.html in a browser and point it at
# ws://<pi-ip>:8000/ws

import asyncio
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="EdgeGuard Dashboard Bridge")

ROOT      = Path(__file__).parent.parent  # repo root
MAIN_PY   = ROOT / "main.py"
PYTHON    = sys.executable

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


async def _run_pipeline():
    """Spawn main.py and forward each stdout line to WebSocket clients."""
    proc = await asyncio.create_subprocess_exec(
        PYTHON, str(MAIN_PY),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(ROOT),
    )
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        await _broadcast(line.decode().strip())


@app.on_event("startup")
async def startup():
    asyncio.create_task(_run_pipeline())


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            await ws.receive_text()   # keep-alive; client sends nothing
    except WebSocketDisconnect:
        _clients.discard(ws)


@app.get("/")
async def serve_dashboard():
    return FileResponse(str(Path(__file__).parent / "index.html"))
