import asyncio
import websockets

async def test_ws():
    uri = "ws://127.0.0.1:8000/ws/2330"
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected to WebSocket")
            # Wait 5 seconds for any message
            try:
                response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                print(f"Received: {response}")
            except asyncio.TimeoutError:
                print("No message received in 5 seconds.")
    except Exception as e:
        print(f"Failed to connect: {e}")

asyncio.run(test_ws())
