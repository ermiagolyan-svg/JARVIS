# JARVIS Real-Time WebSocket Server - Lumia Audio Streaming & Control
# Mounted on the unified FastAPI app (port 8765) at /ws/lumia and /ws/desktop.

import json
import logging
import math
import struct
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure project root is in Python path for imports like `from Network.API...`
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient

_LOG_DIR = Path(__file__).resolve().parent.parent.parent / 'Logs'
_HEARTBEAT_TIMEOUT = 25.0
_LUMIA_PATH = '/ws/lumia'
_DESKTOP_PATH = '/ws/desktop'


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger('jarvis_ws')
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(_LOG_DIR / 'ws_server.log'), encoding='utf-8')
        fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        logger.addHandler(fh)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter('[WS] %(message)s'))
        logger.addHandler(ch)
    return logger


_logger = _setup_logger()


class JarvisWebSocketManager:
    # Manages /ws/lumia (binary 16kHz PCM + JSON) and /ws/desktop (JSON control/HUD).

    def __init__(
        self,
        app: FastAPI,
        voice_orchestrator: Optional[Any] = None,
        auth_manager: Optional[Any] = None,
    ):
        self.app = app
        self._voice_orchestrator = voice_orchestrator
        self._auth_manager = auth_manager
        self._lumia_clients: Dict[str, WebSocket] = {}
        self._desktop_clients: Dict[str, WebSocket] = {}
        self._last_heartbeat: Dict[str, float] = {}
        self._active_mic: str = 'mic'
        self._muted: bool = False
        self._lumia_connected: bool = False
        self._model_loaded: bool = True
        self._pending_confirmations: list = []
        self._last_message: Dict[str, Any] = {}
        self._state: str = 'IDLE'
        self._rms: float = 0.0
        self._telemetry: Dict[str, Any] = {
            'messages_received': 0,
            'audio_chunks_received': 0,
            'heartbeats_received': 0,
        }

        self.app.state.ws_manager = self

        existing_paths = {getattr(r, 'path', '') for r in self.app.routes}
        if _LUMIA_PATH not in existing_paths:
            self.app.add_api_websocket_route(_LUMIA_PATH, self.handle_lumia_connection)
        if _DESKTOP_PATH not in existing_paths:
            self.app.add_api_websocket_route(_DESKTOP_PATH, self.handle_desktop_connection)

    # Aliases for backward compatibility with rest_server wiring
    async def _route_lumia(self, websocket: WebSocket):
        await self.handle_lumia_connection(websocket)

    async def _route_desktop(self, websocket: WebSocket):
        await self.handle_desktop_connection(websocket)

    async def _lumia_ws_endpoint(self, websocket: WebSocket):
        await self.handle_lumia_connection(websocket)

    async def _desktop_ws_endpoint(self, websocket: WebSocket):
        await self.handle_desktop_connection(websocket)

    def _get_auth_manager(self):
        if self._auth_manager is not None:
            return self._auth_manager
        if hasattr(self.app.state, 'auth_manager') and self.app.state.auth_manager is not None:
            return self.app.state.auth_manager
        from Network.Authentication.auth_manager import NetworkAuthManager
        self._auth_manager = NetworkAuthManager()
        return self._auth_manager

    def _get_voice_orchestrator(self):
        if self._voice_orchestrator is not None:
            return self._voice_orchestrator
        if hasattr(self.app.state, 'voice_orchestrator') and self.app.state.voice_orchestrator is not None:
            return self.app.state.voice_orchestrator
        try:
            from Voice.voice_orchestrator import VoiceOrchestrator
            if hasattr(VoiceOrchestrator, 'get_instance'):
                self._voice_orchestrator = VoiceOrchestrator.get_instance()
        except Exception:
            pass
        return self._voice_orchestrator

    def _validate_connection(
        self,
        websocket: WebSocket,
        query_token: Optional[str],
        client_ip: str,
    ) -> bool:
        auth_mgr = self._get_auth_manager()

        if client_ip not in ('0.0.0.0', '127.0.0.1', '::1', 'localhost', 'testclient'):
            if hasattr(auth_mgr, 'validate_source_ip'):
                ip_res = auth_mgr.validate_source_ip(client_ip)
                ip_ok = ip_res.success if hasattr(ip_res, 'success') else bool(ip_res)
                if not ip_ok:
                    _logger.warning('WebSocket rejected: non-LAN IP %s', client_ip)
                    return False

        token = (query_token or '').strip()
        if not token:
            auth_header = websocket.headers.get('authorization', '')
            if auth_header.lower().startswith('bearer '):
                token = auth_header[7:].strip()

        if not token:
            _logger.warning('WebSocket rejected: missing token from %s', client_ip)
            return False

        if hasattr(auth_mgr, 'validate_token'):
            res = auth_mgr.validate_token(token)
            is_valid = res.success if hasattr(res, 'success') else bool(res)
            if is_valid:
                return True

        try:
            from Network.API.rest_server import get_master_token
            master = get_master_token()
            if master and token == master:
                return True
        except Exception:
            pass

        try:
            from Network.Authentication.auth_manager import _load_auth_config
            cfg_token = _load_auth_config().get('master_token', '')
            if cfg_token and token == cfg_token:
                return True
        except Exception:
            pass

        _logger.warning('WebSocket rejected: invalid token from %s', client_ip)
        return False

    def _build_hud_payload(self) -> dict:
        return {
            'type': 'hud_update',
            'state': self._state,
            'rms': round(self._rms, 4),
            'lumia_connected': self._lumia_connected,
            'model_loaded': self._model_loaded,
            'muted': self._muted,
            'active_mic': self._active_mic,
            'telemetry': self._telemetry.copy(),
            'pending_confirmations': list(self._pending_confirmations),
            'last_message': self._last_message if isinstance(self._last_message, dict) else {'text': str(self._last_message or '')},
        }

    async def broadcast_hud_update(self, exclude_client_id: Optional[str] = None):
        payload_str = json.dumps(self._build_hud_payload(), ensure_ascii=False)
        for cid, ws in list(self._lumia_clients.items()) + list(self._desktop_clients.items()):
            if exclude_client_id and cid == exclude_client_id:
                continue
            try:
                await ws.send_text(payload_str)
            except Exception:
                pass

    def _update_mic_state(self, connected: bool):
        if connected:
            self._lumia_connected = True
            self._active_mic = 'network'
            _logger.info('Active mic switched to network (Lumia connected)')
        else:
            self._lumia_connected = False
            self._active_mic = 'mic'
            _logger.warning('Active mic fell back to mic (Lumia disconnected/timeout)')

        vo = self._get_voice_orchestrator()
        if vo is not None:
            for attr in ('set_audio_source', 'set_active_mic', 'switch_audio_source'):
                if hasattr(vo, attr):
                    try:
                        getattr(vo, attr)(self._active_mic)
                    except Exception:
                        pass
                    break

    def check_heartbeat_timeouts(self) -> bool:
        now = time.time()
        timed_out = [
            cid for cid in list(self._lumia_clients.keys())
            if (now - self._last_heartbeat.get(cid, now)) > _HEARTBEAT_TIMEOUT
        ]
        for cid in timed_out:
            _logger.warning('Lumia client %s timed out (>%.0fs)', cid, _HEARTBEAT_TIMEOUT)
            self._cleanup_client(cid, is_lumia=True)
        return len(timed_out) > 0

    async def handle_lumia_connection(self, websocket: WebSocket):
        query_token = websocket.query_params.get('token')
        client_ip = websocket.client.host if websocket.client else '0.0.0.0'

        if not self._validate_connection(websocket, query_token, client_ip):
            await websocket.close(code=4001, reason='Unauthorized')
            return

        await websocket.accept()
        client_id = str(id(websocket))
        self._lumia_clients[client_id] = websocket
        self._last_heartbeat[client_id] = time.time()
        self._update_mic_state(True)
        _logger.info('Lumia client connected: %s', client_id)

        try:
            while True:
                self.check_heartbeat_timeouts()
                if client_id not in self._lumia_clients:
                    break

                try:
                    msg = await websocket.receive()
                except WebSocketDisconnect:
                    break

                msg_type = msg.get('type', '')
                if msg_type == 'websocket.disconnect':
                    break

                self._last_heartbeat[client_id] = time.time()
                self._telemetry['messages_received'] += 1

                if msg.get('bytes') is not None:
                    pcm_bytes = msg.get('bytes')
                    if isinstance(pcm_bytes, (bytes, bytearray)) and len(pcm_bytes) > 0:
                        self._telemetry['audio_chunks_received'] += 1
                        self._process_lumia_audio_chunk(bytes(pcm_bytes))
                elif msg.get('text') is not None:
                    try:
                        data = json.loads(msg['text'])
                        await self._handle_control_command(client_id, data)
                    except (json.JSONDecodeError, TypeError):
                        _logger.warning('Invalid JSON from Lumia client %s', client_id)
        finally:
            self._cleanup_client(client_id, is_lumia=True)

    async def handle_desktop_connection(self, websocket: WebSocket):
        query_token = websocket.query_params.get('token')
        client_ip = websocket.client.host if websocket.client else '0.0.0.0'

        if not self._validate_connection(websocket, query_token, client_ip):
            await websocket.close(code=4001, reason='Unauthorized')
            return

        await websocket.accept()
        client_id = str(id(websocket))
        self._desktop_clients[client_id] = websocket
        self._last_heartbeat[client_id] = time.time()
        _logger.info('Desktop client connected: %s', client_id)

        try:
            while True:
                try:
                    msg = await websocket.receive()
                except WebSocketDisconnect:
                    break

                if msg.get('type') == 'websocket.disconnect':
                    break

                self._last_heartbeat[client_id] = time.time()
                self._telemetry['messages_received'] += 1

                if msg.get('text') is not None:
                    try:
                        data = json.loads(msg['text'])
                        await self._handle_control_command(client_id, data)
                    except (json.JSONDecodeError, TypeError):
                        _logger.warning('Invalid JSON from Desktop client %s', client_id)
        finally:
            self._cleanup_client(client_id, is_lumia=False)

    async def _handle_control_command(self, client_id: str, data: dict):
        cmd = data.get('type')
        if not cmd:
            return

        payload = data.get('payload') if isinstance(data.get('payload'), dict) else {}

        if cmd == 'heartbeat':
            self._telemetry['heartbeats_received'] += 1
            await self.broadcast_hud_update()

        elif cmd == 'mute_toggle':
            if 'muted' in payload:
                self._muted = bool(payload['muted'])
            elif 'mute' in payload:
                self._muted = bool(payload['mute'])
            elif 'muted' in data:
                self._muted = bool(data['muted'])
            elif 'mute' in data:
                self._muted = bool(data['mute'])
            else:
                self._muted = not self._muted

            self._state = 'MUTED' if self._muted else 'IDLE'
            vo = self._get_voice_orchestrator()
            if vo is not None and hasattr(vo, 'set_muted'):
                try:
                    vo.set_muted(self._muted)
                except Exception:
                    pass
            _logger.info('Mute toggled: %s', self._muted)
            await self.broadcast_hud_update()

        elif cmd == 'wake_trigger':
            self._state = 'LISTENING'
            self._last_message = {'role': 'system', 'text': 'wake_trigger'}
            _logger.info('Wake trigger received from %s', client_id)
            await self.broadcast_hud_update()

        elif cmd == 'confirm_action':
            action_id = payload.get('action_id') or data.get('action_id')
            if action_id:
                self._pending_confirmations = [
                    item for item in self._pending_confirmations
                    if (item.get('action_id') if isinstance(item, dict) else item) != action_id
                ]
                _logger.info('Action %s confirmed by %s', action_id, client_id)
            await self.broadcast_hud_update()

        elif cmd == 'text_command':
            text = (payload.get('text') or data.get('text') or '').strip()
            if text:
                self._last_message = {'role': 'user', 'text': text}
                _logger.info('Text command from %s: %s', client_id, text)
            await self.broadcast_hud_update()

        else:
            _logger.warning('Unknown WS command type: %s', cmd)

    def _process_lumia_audio_chunk(self, pcm_bytes: bytes):
        try:
            sample_count = len(pcm_bytes) // 2
            if sample_count > 0:
                check_count = min(sample_count, 1280)
                shorts = struct.unpack(f'<{check_count}h', pcm_bytes[:check_count * 2])
                mean_sq = sum(s * s for s in shorts) / float(check_count)
                self._rms = min(1.0, math.sqrt(mean_sq) / 32768.0)
            else:
                self._rms = 0.0

            if self._muted:
                return

            vo = self._get_voice_orchestrator()
            if vo is not None and hasattr(vo, 'ingest_lumia_audio_chunk'):
                vo.ingest_lumia_audio_chunk(pcm_bytes)
        except Exception as e:
            _logger.error('Audio chunk processing error: %s', e)

    def _cleanup_client(self, client_id: str, is_lumia: bool):
        if is_lumia:
            self._lumia_clients.pop(client_id, None)
            if not self._lumia_clients and self._lumia_connected:
                self._update_mic_state(False)
        else:
            self._desktop_clients.pop(client_id, None)
        self._last_heartbeat.pop(client_id, None)

    def get_state(self) -> dict:
        return self._build_hud_payload()


def _resolve_test_token(app: FastAPI, client: TestClient) -> str:
    try:
        from Network.API.rest_server import get_master_token
        tok = get_master_token()
        if tok:
            return tok
    except Exception:
        pass

    try:
        from Network.Authentication.auth_manager import _load_auth_config
        tok = _load_auth_config().get('master_token', '')
        if tok:
            return tok
    except Exception:
        pass

    ws_mgr: Optional[JarvisWebSocketManager] = getattr(app.state, 'ws_manager', None)
    auth_mgr = ws_mgr._get_auth_manager() if ws_mgr else None
    if auth_mgr is not None:
        for attr in ('master_token', '_master_token', 'active_token'):
            val = getattr(auth_mgr, attr, None)
            if isinstance(val, str) and val:
                return val
        if hasattr(auth_mgr, 'get_pairing_pin'):
            pin = auth_mgr.get_pairing_pin()
            r = client.post('/api/v1/pair', json={'pin': str(pin), 'device_name': 'ws_self_test'})
            if r.status_code == 200:
                return r.json().get('token', '')

    return ''


def _run_self_test() -> bool:
    from Network.API.rest_server import create_rest_app

    app = create_rest_app()
    ws_mgr = getattr(app.state, 'ws_manager', None)
    if ws_mgr is None:
        ws_mgr = JarvisWebSocketManager(app)

    client = TestClient(app)
    test_token = _resolve_test_token(app, client)

    passed = 0
    failed = 0

    def _check(name: str, actual: Any, expected: Any):
        nonlocal passed, failed
        if actual == expected:
            passed += 1
            print(f'  [PASS] {name}')
        else:
            failed += 1
            print(f'  [FAIL] {name} -> got {actual!r}, expected {expected!r}')

    # Test 1: Reject /ws/lumia without token
    try:
        with client.websocket_connect('/ws/lumia') as ws:
            ws.receive_text()
        _check('Lumia no-token rejected', False, True)
    except Exception:
        _check('Lumia no-token rejected', True, True)

    # Test 2: Accept /ws/lumia with valid token + verify automatic mic failover
    try:
        with client.websocket_connect(f'/ws/lumia?token={test_token}') as ws:
            _check('Lumia valid-token connected (active_mic=network)', ws_mgr.get_state()['active_mic'], 'network')
        _check('Lumia disconnect failover (active_mic=mic)', ws_mgr.get_state()['active_mic'], 'mic')
    except Exception as e:
        _check(f'Lumia valid-token & failover ({e})', False, True)

    # Test 3: Reject /ws/desktop with invalid token
    try:
        with client.websocket_connect('/ws/desktop?token=invalid_token_000000') as ws:
            ws.receive_text()
        _check('Desktop bad-token rejected', False, True)
    except Exception:
        _check('Desktop bad-token rejected', True, True)

    # Test 4: JSON heartbeat -> hud_update
    try:
        with client.websocket_connect(f'/ws/lumia?token={test_token}') as ws:
            ws.send_text(json.dumps({'type': 'heartbeat', 'payload': {}}))
            reply = json.loads(ws.receive_text())
            _check('Heartbeat -> hud_update received', reply.get('type'), 'hud_update')
    except Exception as e:
        _check(f'Heartbeat -> hud_update ({e})', False, True)

    # Test 5: Binary 16kHz 16-bit mono PCM chunk (2560 bytes = 80ms)
    try:
        with client.websocket_connect(f'/ws/lumia?token={test_token}') as ws:
            ws.send_bytes(bytes(2560))
            ws.send_text(json.dumps({'type': 'heartbeat', 'payload': {}}))
            reply = json.loads(ws.receive_text())
            chunks = reply.get('telemetry', {}).get('audio_chunks_received', 0)
            _check('Binary 2560-byte PCM frame ingested', chunks >= 1, True)
    except Exception as e:
        _check(f'Binary PCM frame test ({e})', False, True)

    # Test 6: mute_toggle command -> hud_update
    try:
        with client.websocket_connect(f'/ws/lumia?token={test_token}') as ws:
            ws.send_text(json.dumps({'type': 'mute_toggle', 'payload': {'muted': True}}))
            reply = json.loads(ws.receive_text())
            _check('mute_toggle -> muted=True', reply.get('muted'), True)
    except Exception as e:
        _check(f'mute_toggle test ({e})', False, True)

    # Test 7: text_command on /ws/desktop -> hud_update
    try:
        with client.websocket_connect(f'/ws/desktop?token={test_token}') as ws:
            ws.send_text(json.dumps({'type': 'text_command', 'payload': {'text': 'Hello JARVIS'}}))
            reply = json.loads(ws.receive_text())
            _check('Desktop text_command -> hud_update', reply.get('type'), 'hud_update')
    except Exception as e:
        _check(f'Desktop text_command test ({e})', False, True)

    # Test 8: Health endpoint on unified server
    r = client.get('/api/v1/health')
    _check('GET /api/v1/health', r.status_code, 200)

    print(f'\n=== Results: {passed} passed, {failed} failed ===\n')
    return failed == 0


if __name__ == '__main__':
    print('=' * 60)
    print('JARVIS WebSocket Server - Self Test (Stage 5.3)')
    print('=' * 60)
    ok = _run_self_test()
    if not ok:
        print('[FAIL] Self-test had failures.')
        sys.exit(1)
    print('[OK] All Stage 5.3 WebSocket self-tests passed.')
    sys.exit(0)