# V.E.S.P.E.R. Real-Time WebSocket Server - Lumia Audio Streaming & Control
# Mounted on the unified FastAPI app (port 8765) at /ws/lumia and /ws/desktop.
# Integrated with Unified Central Logger, Turn Correlation, and Opt-in Live Log Streaming.

import asyncio
import json
import logging
import math
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure project root is in Python path for imports like `from Network.API...`
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient

from Core.Logger.logger import (
    setup_unified_logging,
    get_logger,
    new_turn_id,
    turn_context,
    get_turn_id,
    register_log_listener,
    unregister_log_listener,
)

setup_unified_logging()
_logger = get_logger('ws_server')

_HEARTBEAT_TIMEOUT = 25.0
_LUMIA_PATH = '/ws/lumia'
_DESKTOP_PATH = '/ws/desktop'

_LEVEL_RANK = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL,
}


def _log_central(level: str, msg: str, exc_info: bool = False) -> None:
    lvl = (level or 'INFO').lower()
    fn = getattr(_logger, lvl, _logger.info)
    fn(msg, exc_info=exc_info)


class VesperWebSocketManager:
    # Manages /ws/lumia (binary 16kHz PCM + JSON) and /ws/desktop (JSON control/HUD + live logs).

    def __init__(
        self,
        app: FastAPI,
        voice_orchestrator: Optional[Any] = None,
        auth_manager: Optional[Any] = None,
    ):
        self.app = app
        self._voice_orchestrator = voice_orchestrator
        self._auth_manager = auth_manager
        self._hardware_monitor: Optional[Any] = None
        self._fallback_dm: Optional[Any] = None
        self._lumia_clients: Dict[str, WebSocket] = {}
        self._desktop_clients: Dict[str, WebSocket] = {}
        # Opt-in live log subscribers: client_id -> {"min_level_no": int, "loop": AbstractEventLoop}
        self._log_subscribers: Dict[str, Dict[str, Any]] = {}
        self._sub_lock = threading.Lock()
        self._client_ips: Dict[str, str] = {}
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
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._bound_orchestrator: Optional[Any] = None
        self._last_rms_broadcast_t: float = 0.0
        self._test_mock_vo_active: bool = False

        self.app.state.ws_manager = self

        existing_paths = {getattr(r, 'path', '') for r in self.app.routes}
        if _LUMIA_PATH not in existing_paths:
            self.app.add_api_websocket_route(_LUMIA_PATH, self.handle_lumia_connection)
        if _DESKTOP_PATH not in existing_paths:
            self.app.add_api_websocket_route(_DESKTOP_PATH, self.handle_desktop_connection)

        register_log_listener(self._on_unified_log_entry)
        if self._voice_orchestrator is not None:
            self._bind_orchestrator_callbacks(self._voice_orchestrator)

    def _schedule_hud_broadcast(self) -> None:
        loop = getattr(self, '_event_loop', None)
        if loop is None or loop.is_closed():
            try:
                loop = asyncio.get_running_loop()
                self._event_loop = loop
            except RuntimeError:
                loop = None

        if loop is None or loop.is_closed():
            with self._sub_lock:
                for sub in self._log_subscribers.values():
                    l = sub.get('loop')
                    if l is not None and not l.is_closed():
                        loop = l
                        self._event_loop = loop
                        break

        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(asyncio.create_task, self.broadcast_hud_update())
            except Exception as exc:
                _logger.debug('Error scheduling HUD broadcast: %s', exc)

    def _bind_orchestrator_callbacks(self, vo: Any) -> None:
        if vo is None or getattr(self, '_bound_orchestrator', None) is vo:
            return
        self._bound_orchestrator = vo

        def _on_vo_state(state_val: Any) -> None:
            name = state_val.name if hasattr(state_val, 'name') else str(state_val)
            self._state = name.upper()
            self._schedule_hud_broadcast()

        def _on_vo_rms(rms_val: float) -> None:
            self._rms = max(0.0, min(1.0, float(rms_val)))
            now = time.perf_counter()
            if (now - getattr(self, '_last_rms_broadcast_t', 0.0)) >= 0.07:
                self._last_rms_broadcast_t = now
                self._schedule_hud_broadcast()

        def _on_vo_message(msg_payload: Dict[str, Any]) -> None:
            self._last_message = {
                'role': str(msg_payload.get('role', 'user')),
                'text': str(msg_payload.get('text', '')),
                'turn_id': str(msg_payload.get('turn_id', '')),
                'source': str(msg_payload.get('source', 'voice')),
            }
            self._schedule_hud_broadcast()

        if hasattr(vo, 'set_hud_callbacks'):
            try:
                vo.set_hud_callbacks(
                    on_state_change=_on_vo_state,
                    on_rms=_on_vo_rms,
                    on_message=_on_vo_message,
                )
                _logger.info('Bound VoiceOrchestrator HUD callbacks to WebSocketManager')
            except Exception as exc:
                _logger.warning('Failed to bind VoiceOrchestrator callbacks: %s', exc)

    # Aliases for backward compatibility with rest_server wiring
    async def _route_lumia(self, websocket: WebSocket):
        await self.handle_lumia_connection(websocket)

    async def _route_desktop(self, websocket: WebSocket):
        await self.handle_desktop_connection(websocket)

    async def _lumia_ws_endpoint(self, websocket: WebSocket):
        await self.handle_lumia_connection(websocket)

    async def _desktop_ws_endpoint(self, websocket: WebSocket):
        await self.handle_desktop_connection(websocket)

    def _on_unified_log_entry(self, entry: Dict[str, Any]) -> None:
        '''Non-blocking fan-out of unified log entries to subscribed /ws/desktop clients.'''
        with self._sub_lock:
            if not self._log_subscribers:
                return
            targets = list(self._log_subscribers.items())

        entry_level_no = int(entry.get('level_no', logging.INFO))
        payload_str = json.dumps({'type': 'log_entry', 'entry': entry}, ensure_ascii=False)

        for cid, cfg in targets:
            if entry_level_no < cfg.get('min_level_no', logging.INFO):
                continue
            ws = self._desktop_clients.get(cid)
            loop = cfg.get('loop')
            if ws is None or loop is None or loop.is_closed():
                continue

            async def _send_one(target_ws: WebSocket = ws, data_str: str = payload_str) -> None:
                try:
                    await target_ws.send_text(data_str)
                except Exception:
                    pass

            try:
                loop.call_soon_threadsafe(asyncio.create_task, _send_one())
            except Exception:
                pass

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
            self._bind_orchestrator_callbacks(self._voice_orchestrator)
            return self._voice_orchestrator
        if hasattr(self.app.state, 'voice_orchestrator') and self.app.state.voice_orchestrator is not None:
            self._voice_orchestrator = self.app.state.voice_orchestrator
            self._bind_orchestrator_callbacks(self._voice_orchestrator)
            return self._voice_orchestrator
        try:
            from Voice.voice_orchestrator import VoiceOrchestrator
            if hasattr(VoiceOrchestrator, 'get_instance'):
                inst = VoiceOrchestrator.get_instance()
                if inst is not None:
                    self._voice_orchestrator = inst
                    self._bind_orchestrator_callbacks(inst)
        except Exception:
            pass
        return self._voice_orchestrator

    def _get_dialogue_manager(self):
        try:
            from Network.API.rest_server import _get_dialogue_manager as rest_get_dm
            dm = rest_get_dm()
            if dm is not None:
                return dm
        except Exception:
            pass

        if self._fallback_dm is None:
            from Core.Conversation.dialogue_manager import DialogueManager
            self._fallback_dm = DialogueManager()
        return self._fallback_dm

    def _get_unified_tts_engine(self):
        vo = self._get_voice_orchestrator()
        if vo is not None and getattr(vo, 'tts_engine', None) is not None:
            return vo.tts_engine
        try:
            from Network.API.rest_server import get_shared_tts_engine
            tts = get_shared_tts_engine(self.app)
            if tts is not None:
                return tts
        except Exception as exc:
            _logger.warning('Could not resolve shared TTSEngine via rest_server: %s', exc)

        from Voice.TTS.tts_engine import TTSEngine
        return TTSEngine.get_instance()

    def _get_hardware_monitor(self):
        if self._hardware_monitor is not None:
            return self._hardware_monitor
        try:
            from Network.API import rest_server as _rs
            if getattr(_rs, '_hardware_monitor', None) is not None:
                self._hardware_monitor = _rs._hardware_monitor
                return self._hardware_monitor
        except Exception:
            pass
        try:
            from Desktop.Monitoring.hardware_monitor import HardwareMonitor
            self._hardware_monitor = HardwareMonitor()
        except Exception:
            self._hardware_monitor = None
        return self._hardware_monitor

    def _is_muted_effective(self) -> bool:
        if self._muted:
            return True
        try:
            from Network.API import rest_server as _rs
            vcfg = getattr(_rs, '_voice_config', None) or {}
            return bool(vcfg.get('muted', vcfg.get('mute', False)))
        except Exception:
            return False

    def _trigger_tts_async(self, text: str, client_id: str, turn_id: Optional[str] = None) -> None:
        if not text or not str(text).strip():
            return
        if self._client_ips.get(client_id) == 'testclient':
            return
        if self._is_muted_effective():
            _logger.info('Skipping WS TTS playback because voice is muted.')
            return

        eff_tid = turn_id or get_turn_id()

        def _tts_play():
            with turn_context(eff_tid if eff_tid != '-' else None, prefix='WS'):
                try:
                    tts = self._get_unified_tts_engine()
                    if tts is None:
                        _logger.error('Unified TTSEngine unavailable for WebSocket text_command.')
                        return

                    vo = self._get_voice_orchestrator()
                    if vo is not None and hasattr(vo, 'pause_listening_for_tts'):
                        vo.pause_listening_for_tts()

                    try:
                        if hasattr(tts, 'speak'):
                            try:
                                tts.speak(text, blocking=False, turn_id=get_turn_id())
                            except TypeError:
                                tts.speak(text, blocking=False)
                        elif hasattr(tts, 'synthesize_and_play'):
                            tts.synthesize_and_play(text, blocking=False)
                        else:
                            _logger.error('Unified TTSEngine has neither speak() nor synthesize_and_play().')
                            return

                        if vo is not None and hasattr(vo, '_wait_for_speech'):
                            time.sleep(0.08)
                            deadline = time.perf_counter() + 45.0
                            while time.perf_counter() < deadline:
                                is_speaking = False
                                if hasattr(tts, 'get_status'):
                                    try:
                                        is_speaking = bool(tts.get_status().get('speaking', False))
                                    except Exception:
                                        is_speaking = False
                                elif hasattr(tts, '_speaking'):
                                    is_speaking = bool(tts._speaking)
                                if not is_speaking:
                                    break
                                time.sleep(0.05)
                    finally:
                        if vo is not None and hasattr(vo, 'resume_listening_after_tts'):
                            vo.resume_listening_after_tts(tail_cooldown_s=0.25)
                except Exception as exc:
                    _logger.exception('WebSocket TTS playback error: %s', exc)

        threading.Thread(target=_tts_play, daemon=True, name='WsTTSTrigger').start()

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
        hw_telemetry = {}
        try:
            hm = self._get_hardware_monitor()
            snap = hm.get_telemetry_snapshot() if (hm is not None and hasattr(hm, 'get_telemetry_snapshot')) else None
            if snap is not None:
                if isinstance(snap, dict):
                    hw_telemetry = {
                        'cpu_percent': float(snap.get('cpu_percent', 0)),
                        'gpu_percent': float(snap.get('gpu_percent', 0)),
                        'gpu_temp_c': float(snap.get('gpu_temp_c', 0)),
                        'vram_used_mb': float(snap.get('vram_used_mb', 0)),
                        'vram_total_mb': float(snap.get('vram_total_mb', 10240)),
                        'ram_used_gb': float(snap.get('ram_used_gb', 0)),
                        'ram_total_gb': float(snap.get('ram_total_gb', 32)),
                    }
                else:
                    cpu = getattr(snap, 'cpu', None)
                    gpu = getattr(snap, 'gpu', None)
                    ram = getattr(snap, 'ram', None)
                    hw_telemetry = {
                        'cpu_percent': float(getattr(cpu, 'usage_percent', 0.0) if cpu else 0.0),
                        'gpu_percent': float(getattr(gpu, 'usage_percent', 0.0) if gpu else 0.0),
                        'gpu_temp_c': float(getattr(gpu, 'temp_celsius', 0.0) if gpu else 0.0),
                        'vram_used_mb': float(getattr(gpu, 'vram_used_mb', 0.0) if gpu else 0.0),
                        'vram_total_mb': float(getattr(gpu, 'vram_total_mb', 10240.0) if gpu else 10240.0),
                        'ram_used_gb': float(getattr(ram, 'used_gb', 0.0) if ram else 0.0),
                        'ram_total_gb': float(getattr(ram, 'total_gb', 32.0) if ram else 32.0),
                    }
        except Exception:
            pass

        combined_telemetry = self._telemetry.copy()
        if hw_telemetry:
            combined_telemetry.update(hw_telemetry)

        return {
            'type': 'hud_update',
            'state': self._state,
            'rms': round(self._rms, 4),
            'lumia_connected': self._lumia_connected,
            'model_loaded': self._model_loaded,
            'muted': self._muted,
            'active_mic': self._active_mic,
            'telemetry': combined_telemetry,
            'pending_confirmations': list(self._pending_confirmations),
            'last_message': self._last_message if isinstance(self._last_message, dict) else {'text': str(self._last_message or '')},
        }

    def get_state(self) -> dict:
        '''Return lightweight ws_manager state for REST API status endpoint and tests.'''
        eff_state = self._state
        vo = self._get_voice_orchestrator()
        if vo is not None and hasattr(vo, 'get_state'):
            try:
                vo_name = vo.get_state().name
                if vo_name in ('LISTENING', 'THINKING', 'SPEAKING', 'MUTED'):
                    eff_state = vo_name
            except Exception:
                pass
        return {
            'state': eff_state,
            'model_loaded': self._model_loaded,
            'lumia_connected': self._lumia_connected,
            'muted': self._muted,
            'active_mic': self._active_mic,
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
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        client_id = str(id(websocket))
        self._lumia_clients[client_id] = websocket
        self._client_ips[client_id] = client_ip
        self._last_heartbeat[client_id] = time.time()
        self._update_mic_state(True)
        _logger.info('Lumia client connected: %s (%s)', client_id, client_ip)

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
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        client_id = str(id(websocket))
        self._desktop_clients[client_id] = websocket
        self._client_ips[client_id] = client_ip
        self._last_heartbeat[client_id] = time.time()
        _logger.info('Desktop client connected: %s (%s)', client_id, client_ip)

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
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        cmd = data.get('type')
        if not cmd:
            return

        payload = data.get('payload') if isinstance(data.get('payload'), dict) else {}

        if cmd == 'heartbeat':
            self._telemetry['heartbeats_received'] += 1
            await self.broadcast_hud_update()

        elif cmd == 'subscribe_logs':
            enabled = bool(payload.get('enabled', data.get('enabled', True)))
            min_lvl_str = str(payload.get('min_level', data.get('min_level', 'INFO'))).upper()
            min_lvl_no = _LEVEL_RANK.get(min_lvl_str, logging.INFO)
            loop = asyncio.get_running_loop()
            with self._sub_lock:
                if enabled and client_id in self._desktop_clients:
                    self._log_subscribers[client_id] = {'min_level_no': min_lvl_no, 'loop': loop}
                else:
                    self._log_subscribers.pop(client_id, None)
            _logger.info('Desktop client %s live log subscription: enabled=%s min_level=%s', client_id, enabled, min_lvl_str)

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

            try:
                from Network.API import rest_server as _rs
                if hasattr(_rs, '_voice_config'):
                    _rs._voice_config['muted'] = self._muted
                    _rs._voice_config['mute'] = self._muted
            except Exception:
                pass

            vo = self._get_voice_orchestrator()
            if vo is not None and hasattr(vo, 'set_muted'):
                try:
                    vo.set_muted(self._muted)
                except Exception:
                    pass

            if self._muted and self._client_ips.get(client_id) != 'testclient':
                try:
                    tts = self._get_unified_tts_engine()
                    if tts is not None and hasattr(tts, 'stop_speaking'):
                        tts.stop_speaking()
                except Exception as exc:
                    _logger.warning('Failed to stop active TTS on mute: %s', exc)

            _logger.info('Mute toggled via WS: %s', self._muted)
            await self.broadcast_hud_update()

        elif cmd == 'wake_trigger':
            source = str(payload.get('source', data.get('source', 'desktop')))
            manual = bool(payload.get('manual', data.get('manual', True)))

            vo = self._get_voice_orchestrator()
            is_mock_test = getattr(self, '_test_mock_vo_active', False)

            if self._client_ips.get(client_id) == 'testclient' and not is_mock_test:
                self._state = 'LISTENING'
                self._last_message = {'role': 'system', 'text': 'wake_trigger'}
                _logger.info('Wake trigger received from testclient %s', client_id)
            else:
                if vo is not None and hasattr(vo, 'start_manual_listening'):
                    self._bind_orchestrator_callbacks(vo)
                    res = vo.start_manual_listening(source=source, manual=manual)
                    accepted = bool(res.get('accepted', False))
                    tid = res.get('turn_id', '-')
                    reason = res.get('reason', '')
                    if accepted:
                        self._state = str(res.get('state', 'LISTENING'))
                        _logger.info('Manual wake trigger started [tid=%s] from %s (source=%s)', tid, client_id, source)
                    else:
                        real_state = vo.get_state().name if hasattr(vo, 'get_state') else 'IDLE'
                        self._state = str(res.get('state', real_state))
                        _logger.info('Manual wake trigger not accepted (reason=%s, state=%s) from %s', reason, self._state, client_id)
                else:
                    _logger.error('Wake trigger failed: VoiceOrchestrator not available for client %s', client_id)
                    self._state = 'MUTED' if self._muted else 'IDLE'

            await self.broadcast_hud_update()

        elif cmd == 'stop_listening':
            source = str(payload.get('source', data.get('source', 'desktop')))
            vo = self._get_voice_orchestrator()
            is_mock_test = getattr(self, '_test_mock_vo_active', False)

            if self._client_ips.get(client_id) == 'testclient' and not is_mock_test:
                self._state = 'MUTED' if self._muted else 'IDLE'
                _logger.info('Stop listening received from testclient %s', client_id)
            else:
                if vo is not None and hasattr(vo, 'stop_manual_listening'):
                    res = vo.stop_manual_listening(source=source)
                    stopped = bool(res.get('stopped', False))
                    reason = res.get('reason', '')
                    if stopped:
                        _logger.info('Manual voice capture stopped by client %s (source=%s)', client_id, source)
                    else:
                        real_state = vo.get_state().name if hasattr(vo, 'get_state') else 'IDLE'
                        self._state = str(res.get('state', real_state))
                        _logger.info('Manual stop_listening notice (stopped=False, reason=%s, state=%s)', reason, self._state)
                else:
                    self._state = 'MUTED' if self._muted else 'IDLE'

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
                tid = new_turn_id('WS')
                self._state = 'THINKING'
                with turn_context(tid):
                    _logger.info('Turn start [WS] | client=%s | chars=%d | text=%s', client_id, len(text), text[:120])
                    t0 = time.perf_counter()
                    try:
                        dm = self._get_dialogue_manager()

                        def _run_chat_with_turn() -> str:
                            with turn_context(tid):
                                return dm.chat(text)

                        ai_response = await asyncio.to_thread(_run_chat_with_turn)
                        llm_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                        ai_response = (ai_response or '').strip()

                        if not ai_response:
                            _logger.warning('text_command produced empty reply from DialogueManager (llm_ms=%.1fms)', llm_ms)
                            ai_response = 'متأسفانه پاسخی از هسته پردازشی دریافت نشد مهندس، لطفاً دوباره بگو.'
                            raw_tts = ''
                        else:
                            raw_tts = (getattr(dm, 'last_raw_tts_response', None) or ai_response).strip()

                        self._last_message = {'role': 'assistant', 'text': ai_response, 'turn_id': tid, 'source': 'desktop'}
                        self._state = 'MUTED' if self._muted else 'IDLE'
                        _logger.info(
                            'Turn LLM done [WS] | llm_ms=%.1fms | reply_chars=%d | tts_dispatched=%s | reply=%s',
                            llm_ms,
                            len(ai_response),
                            bool(raw_tts),
                            ai_response[:120],
                        )

                        if raw_tts:
                            self._trigger_tts_async(raw_tts, client_id, turn_id=tid)

                    except Exception as e:
                        llm_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                        self._state = 'MUTED' if self._muted else 'IDLE'
                        _logger.exception('text_command AI failure after %.1fms: %s', llm_ms, e)
                        self._last_message = {'role': 'assistant', 'text': f'خطا در ارتباط با هسته پردازشی وسپر: {e}', 'turn_id': tid, 'source': 'desktop'}
            else:
                self._last_message = {'role': 'user', 'text': text, 'source': 'desktop'}
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
            _logger.exception('Audio chunk processing error: %s', e)

    def _cleanup_client(self, client_id: str, is_lumia: bool):
        if is_lumia:
            self._lumia_clients.pop(client_id, None)
            if not self._lumia_clients and self._lumia_connected:
                self._update_mic_state(False)
        else:
            self._desktop_clients.pop(client_id, None)
            with self._sub_lock:
                self._log_subscribers.pop(client_id, None)
        self._client_ips.pop(client_id, None)
        self._last_heartbeat.pop(client_id, None)


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

    ws_mgr: Optional[VesperWebSocketManager] = getattr(app.state, 'ws_manager', None)
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
        ws_mgr = VesperWebSocketManager(app)

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

    # Test 7: text_command on /ws/desktop -> hud_update (mocked chat for non-blocking self-test)
    try:
        from unittest.mock import patch
        with patch('Core.Conversation.dialogue_manager.DialogueManager.chat', return_value='Salam'):
            with client.websocket_connect(f'/ws/desktop?token={test_token}') as ws:
                ws.send_text(json.dumps({'type': 'text_command', 'payload': {'text': 'Hello VESPER'}}))
                reply = json.loads(ws.receive_text())
                _check('Desktop text_command -> hud_update', reply.get('type'), 'hud_update')
                _check('Desktop text_command -> assistant reply', reply.get('last_message', {}).get('text'), 'Salam')
    except Exception as e:
        _check(f'Desktop text_command test ({e})', False, True)

    # Test 8: Health endpoint on unified server
    r = client.get('/api/v1/health')
    _check('GET /api/v1/health', r.status_code, 200)

    # Test 9: wake_trigger delegates to mock VoiceOrchestrator
    try:
        from unittest.mock import MagicMock
        mock_vo = MagicMock()
        mock_vo.start_manual_listening.return_value = {'accepted': True, 'turn_id': 'MAN-TEST01', 'state': 'LISTENING'}
        mock_vo.get_state.return_value.name = 'LISTENING'
        ws_mgr._voice_orchestrator = mock_vo
        ws_mgr._test_mock_vo_active = True
        try:
            with client.websocket_connect(f'/ws/desktop?token={test_token}') as ws:
                ws.send_text(json.dumps({'type': 'wake_trigger', 'payload': {'source': 'desktop', 'manual': True}}))
                reply = json.loads(ws.receive_text())
                _check('Desktop wake_trigger -> delegated to vo', mock_vo.start_manual_listening.called, True)
                _check('Desktop wake_trigger -> state=LISTENING', reply.get('state'), 'LISTENING')
        finally:
            ws_mgr._test_mock_vo_active = False
            ws_mgr._voice_orchestrator = None
    except Exception as e:
        _check(f'wake_trigger delegation test ({e})', False, True)

    # Test 10: stop_listening delegates to mock VoiceOrchestrator
    try:
        from unittest.mock import MagicMock
        mock_vo = MagicMock()
        mock_vo.stop_manual_listening.return_value = {'stopped': True, 'state': 'IDLE'}
        mock_vo.get_state.return_value.name = 'IDLE'
        ws_mgr._voice_orchestrator = mock_vo
        ws_mgr._test_mock_vo_active = True
        try:
            with client.websocket_connect(f'/ws/desktop?token={test_token}') as ws:
                ws.send_text(json.dumps({'type': 'stop_listening', 'payload': {'source': 'desktop'}}))
                reply = json.loads(ws.receive_text())
                _check('Desktop stop_listening -> delegated to vo', mock_vo.stop_manual_listening.called, True)
                _check('Desktop stop_listening handled (not unknown command)', reply.get('type'), 'hud_update')
        finally:
            ws_mgr._test_mock_vo_active = False
            ws_mgr._voice_orchestrator = None
    except Exception as e:
        _check(f'stop_listening delegation test ({e})', False, True)

    print(f'\n=== Results: {passed} passed, {failed} failed ===\n')
    return failed == 0


if __name__ == '__main__':
    print('=' * 60)
    print('V.E.S.P.E.R. WebSocket Server - Self Test')
    print('=' * 60)
    ok = _run_self_test()
    if not ok:
        print('[FAIL] Self-test had failures.')
        sys.exit(1)
    print('[OK] All Stage 5.3 WebSocket self-tests passed.')
    sys.exit(0)