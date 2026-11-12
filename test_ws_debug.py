from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
import traceback
import anyio

app = FastAPI()

@app.get('/ws/test')
async def test_ws(websocket: WebSocket):
    print('Endpoint called!')
    await websocket.accept()
    print('Accepted!')
    await websocket.send_text('hello')
    msg = await websocket.receive_text()
    print(f'Received: {msg}')
    await websocket.close()

# Check routes
print('Routes:', [r.path for r in app.routes])

client = TestClient(app)

# Try without context manager - use it directly
ws = client.websocket_connect('/ws/test')
print('WS object:', type(ws))
print('WS scope:', ws.scope.get('type'))

# Manually send connect message
ws.send({"type": "websocket.connect"})
try:
    msg = ws.receive()
    print('Received message:', msg)
except Exception as e:
    print('Exception on receive:', type(e).__name__, str(e))

ws.close(1000)