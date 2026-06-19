# mock_server.py
import asyncio
import json
import random
import time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pathlib import Path

app = FastAPI(title="EdgeGuard Mock Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ModeRequest(BaseModel):
    mode: str

inference_mode = "high_power"
clients = set()

@app.get("/api/mode")
async def get_mode():
    return {"mode": inference_mode}

@app.post("/api/mode")
async def set_mode(req: ModeRequest):
    global inference_mode
    inference_mode = req.mode
    print(f"[Mock Server] Mode updated to: {inference_mode}")
    return {"mode": inference_mode}

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    print(f"[Mock Server] Client connected. Total: {len(clients)}")
    try:
        while True:
            # Keep connection open
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        print(f"[Mock Server] Client disconnected. Total: {len(clients)}")

async def broadcast_telemetry():
    labels = ["normal", "imbalance", "bearing", "looseness"]
    diagnostics = {
        "normal": "Nominal Operation",
        "imbalance": "Rotor Imbalance Detected",
        "bearing": "Bearing Degraded State",
        "looseness": "Base Structural Looseness"
    }
    
    # Active simulation state
    current_label = "normal"
    state_ticks = 0
    anomaly_prob_trend = 0.05
    
    while True:
        if not clients:
            await asyncio.sleep(1.0)
            continue
            
        # Shift simulation states every 40 ticks (~20s)
        state_ticks += 1
        if state_ticks > 40:
            current_label = random.choice(labels)
            state_ticks = 0
            print(f"[Mock Simulation] Transitioned to state: {current_label}")
            
        # Gently ramp up anomaly probability if anomaly state is active
        if current_label == "normal":
            anomaly_prob_trend = max(0.02, anomaly_prob_trend - 0.05)
        else:
            anomaly_prob_trend = min(0.98, anomaly_prob_trend + 0.05)
            
        # Distribute raw probabilities based on active simulation state
        raw_probs = {}
        if current_label == "normal":
            raw_probs["normal"] = 1.0 - anomaly_prob_trend
            raw_probs["imbalance"] = anomaly_prob_trend * 0.4
            raw_probs["bearing"] = anomaly_prob_trend * 0.3
            raw_probs["looseness"] = anomaly_prob_trend * 0.3
        elif current_label == "imbalance":
            raw_probs["normal"] = 1.0 - anomaly_prob_trend
            raw_probs["imbalance"] = anomaly_prob_trend
            raw_probs["bearing"] = 0.0
            raw_probs["looseness"] = 0.0
        elif current_label == "bearing":
            raw_probs["normal"] = 1.0 - anomaly_prob_trend
            raw_probs["imbalance"] = 0.0
            raw_probs["bearing"] = anomaly_prob_trend
            raw_probs["looseness"] = 0.0
        elif current_label == "looseness":
            raw_probs["normal"] = 1.0 - anomaly_prob_trend
            raw_probs["imbalance"] = 0.0
            raw_probs["bearing"] = 0.0
            raw_probs["looseness"] = anomaly_prob_trend

        # Normalize probabilities to sum to 1.0
        total_p = sum(raw_probs.values())
        if total_p > 0:
            raw_probs = {k: v / total_p for k, v in raw_probs.items()}
            
        temp_base = 35.0 if current_label == "normal" else 58.0
        board_temp = temp_base + random.uniform(-1.5, 1.5)
        temp_state = "rising" if board_temp >= 55.0 else "normal"
        
        telemetry = {
            "ts": time.time(),
            "source": "demo_synthetic",
            "label": current_label,
            "diagnostic": diagnostics[current_label],
            "imbalance_prob": round(raw_probs.get("imbalance", 0.0), 4),
            "confidence": round(raw_probs[current_label], 4),
            "temp_state": temp_state,
            "board_temp_c": round(board_temp, 2),
            "raw_probs": {k: round(v, 4) for k, v in raw_probs.items()},
            "energy_waste": round(raw_probs.get("imbalance", 0.0) * 25.0, 1),
            "latency_ms": round(random.uniform(5.5, 7.5), 2),
            "drop_rate_pct": round(random.uniform(0.0, 0.5), 3),
            "n_rows": 200,
            "inference_mode": inference_mode
        }
        
        # Broadcast text payload
        payload_str = json.dumps(telemetry)
        dead = set()
        for client in list(clients):
            try:
                await client.send_text(payload_str)
            except Exception:
                dead.add(client)
        clients.difference_update(dead)
        
        await asyncio.sleep(0.5)

@app.get("/command-center", response_class=HTMLResponse)
async def command_center():
    html_path = Path(__file__).parent / "dashboard" / "edgeguard-command-center.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "dashboard" / "edgeguard-command-center.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(broadcast_telemetry())
