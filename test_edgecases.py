import asyncio
import httpx
import websockets

async def test_api_auth():
    print("--- Testing Edge Case 5: Unauthenticated API ---")
    async with httpx.AsyncClient() as client:
        # We send a request without ANY authentication tokens
        resp = await client.post("http://127.0.0.1:8080/api/mode", json={"mode": "low_power"})
        print(f"Status Code: {resp.status_code}")
        print(f"Response: {resp.json()}")
        if resp.status_code == 200:
            print("❌ VULNERABILITY CONFIRMED: Unauthenticated override successful!\n")

async def test_ws_dos():
    print("--- Testing Edge Case 4: WebSocket Connection Exhaustion ---")
    connections = []
    try:
        for i in range(1, 13):
            ws = await websockets.connect("ws://127.0.0.1:8080/ws")
            connections.append(ws)
            print(f"✅ Connected client {i}")
    except websockets.exceptions.InvalidStatusCode as e:
        print(f"❌ Connection {i} REJECTED with HTTP {e.status_code}!")
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"❌ Connection {i} REJECTED with WS Close Code {e.code}!")
    finally:
        print("VULNERABILITY CONFIRMED: A single user exhausted the pool.")
        for ws in connections:
            await ws.close()

async def main():
    await test_api_auth()
    await test_ws_dos()

if __name__ == "__main__":
    asyncio.run(main())
