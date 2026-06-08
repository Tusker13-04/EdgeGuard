import pytest
import asyncio
from unittest.mock import AsyncMock, patch
from dashboard.server import app, set_mode, ModeRequest, websocket_endpoint, get_mode

@pytest.mark.asyncio
async def test_get_mode():
    response = await get_mode()
    import json
    body = json.loads(response.body)
    assert "mode" in body

@pytest.mark.asyncio
async def test_set_mode():
    req = ModeRequest(mode="low_power")
    response = await set_mode(req)
    import json
    body = json.loads(response.body)
    assert body["mode"] == "low_power"

@pytest.mark.asyncio
async def test_websocket_endpoint():
    # Mock a websocket
    ws_mock = AsyncMock()
    ws_mock.headers = {}
    
    # We will cancel the endpoint after it accepts
    import dashboard.server
    
    # Add to clients to ensure it behaves normally
    # We need to raise a WebSocketDisconnect to exit the loop
    from fastapi import WebSocketDisconnect
    ws_mock.receive_text.side_effect = WebSocketDisconnect()
    
    await websocket_endpoint(ws_mock)
    
    ws_mock.accept.assert_awaited_once()
    assert ws_mock not in dashboard.server._clients

@pytest.mark.asyncio
async def test_websocket_max_clients():
    import dashboard.server
    dashboard.server._MAX_CLIENTS = 1
    
    ws_mock1 = AsyncMock()
    ws_mock1.headers = {}
    dashboard.server._clients.add(ws_mock1)
    
    ws_mock2 = AsyncMock()
    ws_mock2.headers = {}
    
    await websocket_endpoint(ws_mock2)
    
    ws_mock2.close.assert_awaited_once_with(code=1008)
    dashboard.server._clients.discard(ws_mock1)

@pytest.mark.asyncio
async def test_broadcast():
    import dashboard.server
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    dashboard.server._clients = {ws1, ws2}
    
    await dashboard.server._broadcast("test_msg")
    
    ws1.send_text.assert_awaited_once_with("test_msg")
    ws2.send_text.assert_awaited_once_with("test_msg")
    
    dashboard.server._clients.clear()

@pytest.mark.asyncio
async def test_broadcast_worker():
    import dashboard.server
    dashboard.server._telemetry_queue = asyncio.Queue(maxsize=50)
    # enqueue something
    await dashboard.server._telemetry_queue.put('{"val": 1}')
    
    # run worker as task and then cancel it
    worker_task = asyncio.create_task(dashboard.server._broadcast_worker())
    
    # Wait a tiny bit for it to process
    await asyncio.sleep(0.1)
    
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    
    # queue should be empty
    assert dashboard.server._telemetry_queue.empty()
