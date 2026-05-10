#!/usr/bin/env python3
"""
EdgeGuard — pi/server.py
========================
FastAPI WebSocket bridge + static file server.

What it does
------------
1. Launches main.py as a subprocess and reads its stdout line-by-line.
2. Parses each line as a JSON frame.
3. Broadcasts the (re-serialised with orjson) frame to all connected WebSocket
   clients at ws://host:8765/ws.
4. Updates the ws_clients count in each outgoing frame so the dashboard shows
   how many browser tabs are connected.
5. Serves dashboard/dashboard.html as a static file at GET / so the user can
   open the dashboard without any extra HTTP server.

Design notes
------------
- asyncio throughout — no extra threads; both the subprocess reader and all
  WebSocket connections share the same event loop.
- subprocess stdout is read with asyncio.StreamReader (non-blocking).
- Connected clients are stored in a plain Python set; add/remove under no lock
  because all mutations happen on the single event-loop thread.
- If main.py crashes, server.py auto-restarts it after RESTART_DELAY_S seconds.
- orjson is used for all serialisation — 3–5× faster than stdlib json on Pi.

Usage
-----
    python pi/server.py [--host 0.0.0.0] [--port 8765]
                        [--main-args "--demo"]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Set

try:
    import orjson
    def _dumps(obj: dict) -> bytes:
        return orjson.dumps(obj)
except ImportError:
    def _dumps(obj: dict) -> bytes:
        return json.dumps(obj).encode()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_HOST      = "0.0.0.0"
DEFAULT_PORT      = 8765
RESTART_DELAY_S   = 3.0        # seconds to wait before restarting main.py
MAX_QUEUE_SIZE    = 10         # per-client async send queue depth
HEALTH_INTERVAL_S = 5.0       # how often to send a synthetic heartbeat if no data

# Resolve paths relative to this file — works regardless of cwd
_THIS_DIR    = Path(__file__).parent.resolve()
_REPO_ROOT   = _THIS_DIR.parent
_MAIN_PY     = _THIS_DIR / "main.py"
_DASHBOARD   = _REPO_ROOT / "dashboard" / "dashboard.html"

# ─────────────────────────────────────────────────────────────────────────────
# Application state
# ─────────────────────────────────────────────────────────────────────────────
class AppState:
    def __init__(self) -> None:
        self.clients:   Set[WebSocket]  = set()
        self.last_frame: dict | None    = None
        self.main_args: list[str]       = []

app_state = AppState()

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="EdgeGuard", docs_url=None, redoc_url=None)

@app.get("/")
async def serve_dashboard():
    """Serve the dashboard HTML directly — no separate HTTP server needed."""
    if _DASHBOARD.is_file():
        return FileResponse(str(_DASHBOARD), media_type="text/html")
    return JSONResponse({"error": "dashboard/dashboard.html not found"}, status_code=404)

@app.get("/health")
async def health():
    return {"status": "ok", "clients": len(app_state.clients)}

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    app_state.clients.add(ws)
    try:
        # Immediately push the last known frame so the dashboard doesn't wait
        if app_state.last_frame is not None:
            frame = _inject_ws_count(app_state.last_frame)
            await ws.send_bytes(_dumps(frame))

        # Keep the connection open; client sends nothing back in this protocol
        while True:
            try:
                # We call receive_text with a generous timeout so we detect
                # client disconnects promptly even if no data is flowing.
                await asyncio.wait_for(ws.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Ping to check liveness
                await ws.send_bytes(_dumps({"ping": True}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        app_state.clients.discard(ws)


def _inject_ws_count(frame: dict) -> dict:
    """Shallow-copy and inject the live client count."""
    f = dict(frame)  # shallow — we only mutate the system sub-dict
    f["system"] = dict(frame.get("system", {}))
    f["system"]["ws_clients"] = len(app_state.clients)
    return f


async def _broadcast(raw_bytes: bytes) -> None:
    """Send raw_bytes to all connected clients; remove any that fail."""
    dead: list[WebSocket] = []
    for client in list(app_state.clients):
        try:
            await client.send_bytes(raw_bytes)
        except Exception:
            dead.append(client)
    for d in dead:
        app_state.clients.discard(d)


# ─────────────────────────────────────────────────────────────────────────────
# main.py subprocess manager
# ─────────────────────────────────────────────────────────────────────────────
async def run_main_py(extra_args: list[str]) -> None:
    """Runs main.py as a subprocess, reads stdout lines, broadcasts JSON.
    Restarts automatically if the process exits."""
    cmd = [sys.executable, str(_MAIN_PY)] + extra_args

    while True:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,   # swallow to avoid blocking
        )

        # Log stderr in background without blocking the event loop
        asyncio.create_task(_drain_stderr(proc))

        assert proc.stdout is not None

        while True:
            line = await proc.stdout.readline()
            if not line:
                break   # process closed stdout

            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                frame = json.loads(line_str)
            except json.JSONDecodeError:
                # Not a JSON line (e.g. startup banner from stderr piped) — skip
                continue

            app_state.last_frame = frame
            enriched  = _inject_ws_count(frame)
            raw_bytes = _dumps(enriched)
            await _broadcast(raw_bytes)

        # Process ended
        retcode = await proc.wait()
        print(
            f"[server] main.py exited (code {retcode}). "
            f"Restarting in {RESTART_DELAY_S}s…",
            file=sys.stderr,
        )
        await asyncio.sleep(RESTART_DELAY_S)


async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
    """Read and print stderr so the terminal shows main.py diagnostics."""
    assert proc.stderr is not None
    while True:
        line = await proc.stderr.readline()
        if not line:
            break
        print("[main.py]", line.decode("utf-8", errors="replace").rstrip(), file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# Startup hook — launch main.py when uvicorn starts
# ─────────────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_main_py(app_state.main_args))


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="EdgeGuard WebSocket bridge")
    parser.add_argument("--host",      default=DEFAULT_HOST)
    parser.add_argument("--port",      type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--main-args",
        default="",
        help="Extra args forwarded to main.py, e.g. \"--demo\" or \"--model pi/model.onnx\"",
    )
    args = parser.parse_args()

    app_state.main_args = args.main_args.split() if args.main_args.strip() else []

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",     # keep terminal clean; only warnings+ shown
        access_log=False,
    )


if __name__ == "__main__":
    main()
