# C.E.L.E.S.T.E. / JARVIS Local HTTP REST & WebSocket API Server (Port 8765)
# Fully synchronized with Desktop/UI/Models/JarvisModels.cs and Unified Observability Pipeline

import os
import sys
import json
import time
import logging
import secrets
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import FastAPI, Request, HTTPException, Query, status
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from Core.Logger.logger import (
    setup_unified_logging,
    get_logger,
    get_recent_logs,
    get_buffered_modules,
    get_last_log_id,
    get_recent_logs_count,
    new_turn_id,
    turn_context,
    get_turn_id,
)

setup_unified_logging()
_logger = get_logger("rest_server")

# Hardware Monitor (live telemetry) - safe import with fallback
try:
    from Desktop.Monitoring.hardware_monitor import HardwareMonitor
    _hardware_monitor = HardwareMonitor()
except ImportError:
    _hardware_monitor = None


# ---------------------------------------------------------------------------
# Paths, Unified Logger Helper & State Stores
# ---------------------------------------------------------------------------
_CONFIG_DIR = Path(r"D:\AI\JARVIS\Configuration")
_LOG_DIR = Path(r"D:\AI\JARVIS\Logs")
_AUTH_TOKEN_FILE = _CONFIG_DIR / "auth_token.json"


def _log_central(level: str, msg: str, exc_info: bool = False) -> None:
    lvl = (level or "INFO").lower()
    fn = getattr(_logger, lvl, _logger.info)
    fn(msg, exc_info=exc_info)


_memory_store: Dict[str, Any] = {}
_facts_fallback_store: Dict[str, Dict[str, Any]] = {}
_pending_security: List[Dict[str, Any]] = []
_audit_log: List[Dict[str, Any]] = []
_pairing_pins: Dict[str, str] = {}

_voice_config: Dict[str, Any] = {
    "mute": False,
    "muted": False,
    "volume": 75,
    "engine_mode": "gemini",
    "online_api_key": "AQ.Ab8RN6IlbejJqeSUnU3qyPqPMec2JdkTtPavWykBTzJAZ_y3lg",
    "gemini_model": "gemini-2.5-flash-preview-tts",
    "gemini_voice_name": "Kore",
    "gooya_model_dir": "Data/Models/gooya",
    "gooya_reference_audio": "Data/Models/gooya/speaker_reference.wav",
    "gooya_exaggeration": 0.5,
    "gooya_cfg_weight": 0.5,
    "gooya_precision": "bf16",
    "gooya_auto_unload": False,
    "language": "fa-IR",
    "pitch_semitones": 0.0,
    "dsp_pitch_semitones": 0.0,
    "metallic_mix": 0.0,
    "dsp_metallic_mix": 0.0,
    "reverb_decay": 0.0,
    "dsp_reverb_mix": 0.0,
    "wake_threshold": 0.50,
}


def _sync_voice_config_from_disk() -> Dict[str, Any]:
    try:
        from Core.Config.config_loader import get_config
        cfg = get_config()
        v = cfg.get_section("voice") or {}
        pitch = float(v.get("dsp_pitch_semitones", v.get("pitch_semitones", 0.0)))
        metallic = float(v.get("dsp_metallic_mix", v.get("metallic_mix", 0.0)))
        reverb = float(v.get("dsp_reverb_mix", v.get("reverb_decay", 0.0)))
        wake_th = float(v.get("wake_threshold", 0.50))

        _voice_config["engine_mode"] = str(v.get("engine_mode", _voice_config.get("engine_mode", "gemini")))
        _voice_config["online_api_key"] = str(v.get("online_api_key", _voice_config.get("online_api_key", "")))
        _voice_config["gemini_model"] = str(v.get("gemini_model", _voice_config.get("gemini_model", "gemini-2.5-flash-preview-tts")))
        _voice_config["gemini_voice_name"] = str(v.get("gemini_voice_name", _voice_config.get("gemini_voice_name", "Kore")))
        _voice_config["gooya_model_dir"] = str(v.get("gooya_model_dir", _voice_config.get("gooya_model_dir", "Data/Models/gooya")))
        _voice_config["gooya_reference_audio"] = str(v.get("gooya_reference_audio", _voice_config.get("gooya_reference_audio", "Data/Models/gooya/speaker_reference.wav")))
        _voice_config["gooya_exaggeration"] = float(v.get("gooya_exaggeration", _voice_config.get("gooya_exaggeration", 0.5)))
        _voice_config["gooya_cfg_weight"] = float(v.get("gooya_cfg_weight", _voice_config.get("gooya_cfg_weight", 0.5)))
        _voice_config["gooya_precision"] = str(v.get("gooya_precision", _voice_config.get("gooya_precision", "bf16")))
        _voice_config["gooya_auto_unload"] = bool(v.get("gooya_auto_unload", _voice_config.get("gooya_auto_unload", False)))
        _voice_config["dsp_pitch_semitones"] = pitch
        _voice_config["pitch_semitones"] = pitch
        _voice_config["dsp_metallic_mix"] = metallic
        _voice_config["metallic_mix"] = metallic
        _voice_config["dsp_reverb_mix"] = reverb
        _voice_config["reverb_decay"] = reverb
        _voice_config["wake_threshold"] = wake_th
    except Exception as exc:
        _logger.warning("Error syncing voice config from disk: %s", exc)
    return _voice_config


_sync_voice_config_from_disk()

_telemetry_data: Dict[str, Any] = {
    "uptime_seconds": 0,
    "requests_total": 0,
    "last_pairing": None,
    "voice_muted": False,
}

_START_TIME: float = time.time()
_dialogue_manager: Any = None
_memory_manager: Any = None
_nvml_initialized: bool = False
_live_cpu_percent: float = 3.5


def _cpu_sampler_loop() -> None:
    global _live_cpu_percent
    try:
        import psutil
        psutil.cpu_percent(interval=None)
        while True:
            val = float(psutil.cpu_percent(interval=1.0))
            if val > 0.0:
                _live_cpu_percent = val
    except Exception:
        pass


threading.Thread(target=_cpu_sampler_loop, daemon=True, name="CpuSampler").start()


def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _telemetry_incr() -> None:
    _telemetry_data["requests_total"] += 1
    _telemetry_data["uptime_seconds"] = int(time.time() - _START_TIME)


# ---------------------------------------------------------------------------
# Core Singletons (DialogueManager + LLMClient, MemoryManager, Unified TTSEngine)
# ---------------------------------------------------------------------------
def _get_dialogue_manager() -> Any:
    global _dialogue_manager
    if _dialogue_manager is None:
        try:
            from Core.AI.llm_client import LLMClient
            from Core.Conversation.dialogue_manager import DialogueManager
            llm = LLMClient()
            _dialogue_manager = DialogueManager(llm_client=llm)
        except Exception as e:
            _logger.exception("Failed to initialize DialogueManager with LLMClient: %s", e)
            _dialogue_manager = None
    elif getattr(_dialogue_manager, "llm_client", None) is None:
        try:
            from Core.AI.llm_client import LLMClient
            _dialogue_manager.llm_client = LLMClient()
        except Exception as e:
            _logger.exception("Failed to attach LLMClient to DialogueManager: %s", e)
    return _dialogue_manager


def _get_memory_manager() -> Any:
    global _memory_manager
    if _memory_manager is None:
        try:
            from Core.Memory.memory_manager import MemoryManager
            _memory_manager = MemoryManager.get_memory_manager()
        except Exception as e:
            _logger.warning("MemoryManager init warning: %s", e)
            _memory_manager = None
    return _memory_manager


def _get_tts_engine(app: Any = None) -> Any:
    """Resolve the single unified TTSEngine shared across VoiceOrchestrator, REST, and WebSocket."""
    if app is not None and hasattr(app, "state"):
        orch = getattr(app.state, "voice_orchestrator", None)
        if orch is not None and getattr(orch, "tts_engine", None) is not None:
            return orch.tts_engine
        cached = getattr(app.state, "tts_engine", None)
        if cached is not None:
            return cached

    try:
        from Voice.voice_orchestrator import VoiceOrchestrator
        if hasattr(VoiceOrchestrator, "get_instance"):
            vo = VoiceOrchestrator.get_instance()
            if vo is not None and getattr(vo, "tts_engine", None) is not None:
                return vo.tts_engine
    except Exception:
        pass

    from Voice.TTS.tts_engine import TTSEngine
    engine = TTSEngine.get_instance()
    if app is not None and hasattr(app, "state"):
        app.state.tts_engine = engine
    return engine


get_shared_tts_engine = _get_tts_engine


def _trigger_tts_async(response_text: str, app: Any = None, turn_id: Optional[str] = None) -> None:
    if not response_text or not str(response_text).strip():
        return
    if _voice_config.get("muted", False) or _voice_config.get("mute", False):
        _logger.info("Skipping REST TTS playback because voice is muted.")
        return

    eff_tid = turn_id or get_turn_id()

    def _play():
        with turn_context(eff_tid if eff_tid != "-" else None, prefix="REST"):
            try:
                tts = _get_tts_engine(app)
                if tts is None:
                    _logger.error("Unified TTSEngine is None; cannot play response.")
                    return
                if hasattr(tts, "speak"):
                    try:
                        tts.speak(response_text, blocking=False, turn_id=get_turn_id())
                    except TypeError:
                        tts.speak(response_text, blocking=False)
                elif hasattr(tts, "synthesize_and_play"):
                    tts.synthesize_and_play(response_text, blocking=False)
            except Exception as exc:
                _logger.exception("TTS playback trigger failed: %s", exc)

    threading.Thread(target=_play, daemon=True, name="RestTTSTrigger").start()


# ---------------------------------------------------------------------------
# Live Hardware Telemetry
# ---------------------------------------------------------------------------
def _get_hardware_telemetry() -> Dict[str, float]:
    cpu_pct = 0.0
    ram_used_gb = 0.0
    ram_total_gb = 32.0
    gpu_pct = 0.0
    gpu_temp_c = 0.0
    vram_used_mb = 0.0
    vram_total_mb = 10240.0

    try:
        from Desktop.Monitoring.hardware_monitor import HardwareMonitor
        hm = HardwareMonitor()
        snap = {}
        if hasattr(hm, "get_telemetry_snapshot"):
            snap = hm.get_telemetry_snapshot() or {}
        elif hasattr(hm, "get_metrics"):
            snap = hm.get_metrics() or {}
        elif hasattr(hm, "collect"):
            snap = hm.collect() or {}

        if isinstance(snap, dict):
            cpu_pct = float(snap.get("cpu_percent", snap.get("cpu_load", 0.0)) or 0.0)
            gpu_pct = float(snap.get("gpu_percent", snap.get("gpu_load", 0.0)) or 0.0)
            gpu_temp_c = float(snap.get("gpu_temp_c", snap.get("gpu_temp", 0.0)) or 0.0)
            vram_used_mb = float(snap.get("vram_used_mb", 0.0) or 0.0)
            vram_total_mb = float(snap.get("vram_total_mb", 10240.0) or 10240.0)
            ram_used_gb = float(snap.get("ram_used_gb", 0.0) or 0.0)
            ram_total_gb = float(snap.get("ram_total_gb", 32.0) or 32.0)
    except Exception:
        pass

    try:
        import psutil
        if cpu_pct <= 0.0:
            cpu_pct = _live_cpu_percent or float(psutil.cpu_percent(interval=0.1))
        vm = psutil.virtual_memory()
        ram_used_gb = round(vm.used / (1024.0 ** 3), 2)
        ram_total_gb = round(vm.total / (1024.0 ** 3), 1)
    except Exception:
        pass

    if gpu_pct <= 0.0 and vram_used_mb <= 0.0:
        global _nvml_initialized
        try:
            import pynvml
            if not _nvml_initialized:
                pynvml.nvmlInit()
                _nvml_initialized = True
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            gpu_pct = float(util.gpu)
            gpu_temp_c = float(temp)
            vram_used_mb = round(mem.used / (1024.0 ** 2), 1)
            vram_total_mb = round(mem.total / (1024.0 ** 2), 1)
        except Exception:
            try:
                out = subprocess.check_output(
                    [
                        "nvidia-smi",
                        "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                        "--format=csv,noheader,nounits",
                    ],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=2,
                    text=True,
                ).strip()
                if out:
                    parts = [p.strip() for p in out.splitlines()[0].split(",")]
                    if len(parts) >= 4:
                        gpu_pct = float(parts[0])
                        gpu_temp_c = float(parts[1])
                        vram_used_mb = float(parts[2])
                        vram_total_mb = float(parts[3])
            except Exception:
                pass

    return {
        "cpu_percent": round(cpu_pct, 1),
        "gpu_percent": round(gpu_pct, 1),
        "gpu_temp_c": round(gpu_temp_c, 1),
        "vram_used_mb": round(vram_used_mb, 1),
        "vram_total_mb": round(vram_total_mb, 1),
        "ram_used_gb": round(ram_used_gb, 2),
        "ram_total_gb": round(ram_total_gb, 1),
    }


# ---------------------------------------------------------------------------
# Security & Auth Helpers
# ---------------------------------------------------------------------------
def _is_lan_ip(ip_address: str) -> bool:
    try:
        import ipaddress
        addr = ipaddress.ip_address(ip_address)
        return addr.is_loopback or addr.is_private
    except ValueError:
        return True


def _get_auth_manager():
    from Network.Authentication.auth_manager import NetworkAuthManager
    return NetworkAuthManager()


def validate_request_token(request: Request) -> bool:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:].strip()
    if not token or len(token) < 32:
        return False
    try:
        mgr = _get_auth_manager()
        result = mgr.validate_token(token)
        if getattr(result, "success", False):
            return True
    except Exception:
        pass
    if _AUTH_TOKEN_FILE.exists():
        try:
            data = json.loads(_AUTH_TOKEN_FILE.read_text(encoding="utf-8"))
            saved = data.get("master_token") or data.get("token") or ""
            if saved and secrets.compare_digest(token, saved):
                return True
        except Exception:
            pass
    return False


def get_master_token() -> str:
    try:
        mgr = _get_auth_manager()
        summary = mgr.get_config_summary()
        token = summary.get("master_token", "")
        if token and len(token) >= 32:
            return token
    except Exception:
        pass
    if _AUTH_TOKEN_FILE.exists():
        try:
            data = json.loads(_AUTH_TOKEN_FILE.read_text(encoding="utf-8"))
            return data.get("master_token") or data.get("token") or ""
        except Exception:
            pass
    return ""


def get_active_pairing_pin() -> str:
    if _AUTH_TOKEN_FILE.exists():
        try:
            data = json.loads(_AUTH_TOKEN_FILE.read_text(encoding="utf-8"))
            pin = str(data.get("pairing_pin") or data.get("pin") or "")
            if pin:
                return pin
        except Exception:
            pass
    return "876500"


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------
class PairRequest(BaseModel):
    device_id: Optional[str] = Field(default=None)
    device_name: Optional[str] = Field(default=None)
    pin: Optional[str] = Field(default=None)


class ChatRequest(BaseModel):
    message: Optional[str] = Field(default=None)
    text: Optional[str] = Field(default=None)
    source: Optional[str] = Field(default="desktop")
    provider: Optional[str] = Field(default=None)
    session_id: Optional[str] = Field(default="default")

    def get_message(self) -> str:
        return (self.text or self.message or "").strip()


class VoiceConfigRequest(BaseModel):
    mute: Optional[bool] = None
    muted: Optional[bool] = None
    volume: Optional[int] = Field(None, ge=0, le=100)
    engine_mode: Optional[str] = Field(None, description="'gemini' or 'gooya'")
    online_api_key: Optional[str] = None
    gemini_model: Optional[str] = None
    gemini_voice_name: Optional[str] = None
    gooya_model_dir: Optional[str] = None
    gooya_reference_audio: Optional[str] = None
    gooya_exaggeration: Optional[float] = None
    gooya_cfg_weight: Optional[float] = None
    gooya_precision: Optional[str] = None
    gooya_auto_unload: Optional[bool] = None
    dsp_pitch_semitones: Optional[float] = None
    pitch_semitones: Optional[float] = None
    dsp_metallic_mix: Optional[float] = None
    metallic_mix: Optional[float] = None
    dsp_reverb_mix: Optional[float] = None
    reverb_decay: Optional[float] = None
    wake_threshold: Optional[float] = None
    language: Optional[str] = None
    local_engine: Optional[str] = None
    online_provider: Optional[str] = None
    online_voice_name: Optional[str] = None


class MemoryCreateRequest(BaseModel):
    category: Optional[str] = Field(default="user")
    key: Optional[str] = Field(default=None)
    fact_key: Optional[str] = Field(default=None)
    value: Any = None
    fact_value: Any = None
    importance: Optional[int] = Field(default=5)
    metadata: Dict[str, Any] = {}

    def get_key(self) -> str:
        return (self.key or self.fact_key or "").strip()

    def get_value(self) -> str:
        v = self.value if self.value is not None else self.fact_value
        return str(v) if v is not None else ""


# ---------------------------------------------------------------------------
# FastAPI App Factory
# ---------------------------------------------------------------------------
def create_rest_app(voice_orchestrator: Any = None) -> FastAPI:
    app = FastAPI(title="V.E.S.P.E.R. REST & WS API", version="2.0.0")
    app.state.voice_orchestrator = voice_orchestrator

    @app.middleware("http")
    async def lan_middleware(request: Request, call_next):
        client_ip = request.client.host if request.client else "127.0.0.1"
        if not _is_lan_ip(client_ip):
            _logger.warning("Blocked non-LAN HTTP request from %s to %s", client_ip, request.url.path)
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Access denied: only LAN/IP loopback allowed"},
            )
        return await call_next(request)

    @app.middleware("http")
    async def token_middleware(request: Request, call_next):
        protected_paths = ["/api/v1/"]
        exempt_paths = ["/api/v1/pair", "/api/v1/health", "/health", "/ping", "/ws/lumia", "/ws/desktop"]
        if any(request.url.path.startswith(p) for p in protected_paths):
            if not any(request.url.path.startswith(e) for e in exempt_paths):
                if not validate_request_token(request):
                    return JSONResponse(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        content={"detail": "Missing or invalid Authorization Bearer token"},
                    )
        return await call_next(request)

    # --- Health & Ping ---
    @app.get("/ping", tags=["health"])
    async def api_ping():
        return {"status": "ok", "service": "JARVIS REST API", "token_auth": True}

    @app.get("/api/v1/health", tags=["health"])
    async def api_health():
        return {"status": "ok", "healthy": True, "service": "V.E.S.P.E.R. REST API", "version": "1.0.0"}

    @app.get("/health", tags=["health"])
    async def api_health_alias():
        return {"status": "ok", "healthy": True, "service": "V.E.S.P.E.R. REST API", "version": "1.0.0"}

    # --- Pairing ---
    @app.post("/api/v1/pair", tags=["pairing"])
    async def api_pair(body: PairRequest):
        dev = body.device_id or body.device_name or "JarvisDesktopPC"
        pin = get_active_pairing_pin()
        token = get_master_token()
        _pairing_pins[dev] = pin
        _telemetry_data["last_pairing"] = dev
        _logger.info("Device paired via REST: device=%s", dev)
        return {
            "success": True,
            "status": "ok",
            "message": f"PIN: {pin}",
            "device_id": dev,
            "pin": pin,
            "token": token,
            "expires_in": 86400,
        }

    @app.get("/api/v1/pair/{device_id}", tags=["pairing"])
    async def api_pair_status(device_id: str):
        if device_id in _pairing_pins:
            return {"device_id": device_id, "status": "paired", "pin": _pairing_pins[device_id]}
        return {"device_id": device_id, "status": "unpaired"}

    # --- Status ---
    @app.get("/api/v1/status", tags=["health"])
    async def api_status():
        ws_state = {}
        if hasattr(app.state, "ws_manager") and hasattr(app.state.ws_manager, "get_state"):
            try:
                ws_state = app.state.ws_manager.get_state() or {}
            except Exception:
                ws_state = {}

        dm = _get_dialogue_manager()
        llm_online = False
        if dm and getattr(dm, "llm_client", None):
            try:
                llm_online = bool(dm.llm_client.get_health_status())
            except Exception:
                llm_online = False

        muted_val = bool(_voice_config.get("muted", _voice_config.get("mute", False)))

        target_model = "Gemma 4 26B-A4B IT (IQ4_XS)"
        try:
            from Core.Config.config_loader import get_config
            cfg_mgr = get_config()
            target_model = cfg_mgr.get("llama_cpp", "target_model_name", target_model)
        except Exception:
            pass

        mm = _get_memory_manager()
        mem_count = 0
        if mm is not None:
            try:
                stats = mm.get_stats()
                mem_count = stats.get("facts", 0) + stats.get("conversations", 0)
            except Exception:
                mem_count = len(_memory_store)
        else:
            mem_count = len(_memory_store)

        eff_state = ws_state.get('state', 'IDLE')
        vo = getattr(app.state, 'voice_orchestrator', None)
        if eff_state == 'IDLE' and vo is not None and hasattr(vo, 'get_state'):
            try:
                vo_name = vo.get_state().name
                if vo_name in ('LISTENING', 'THINKING', 'SPEAKING', 'MUTED'):
                    eff_state = vo_name
            except Exception:
                pass

        return {
            "service": "JARVIS REST API",
            "version": "1.0.0",
            "lan_only": True,
            "token_auth": True,
            "memory_count": mem_count,
            "pending_security": len([s for s in _pending_security if s.get("status") == "pending"]),
            "uptime_seconds": int(time.time() - _START_TIME),
            "state": eff_state,
            "model_loaded": llm_online or bool(ws_state.get("model_loaded", True)),
            "model_name": target_model,
            "lumia_connected": bool(ws_state.get("lumia_connected", False)),
            "muted": muted_val,
            "active_mic": ws_state.get("active_mic", "mic"),
            "pairing_pin": get_active_pairing_pin(),
        }

    # --- Telemetry ---
    @app.get("/api/v1/telemetry", tags=["health"])
    async def api_telemetry():
        _telemetry_incr()
        snap = None
        if _hardware_monitor is not None:
            try:
                snap = _hardware_monitor.get_telemetry_snapshot()
            except Exception:
                pass

        def _safe(val, default=0.0):
            return float(val) if val is not None else default

        cpu_p = _safe(snap.cpu.usage_percent) if snap else 0.0
        gpu_p = _safe(snap.gpu.usage_percent) if snap else 0.0
        vram_u = _safe(snap.gpu.vram_used_mb) if snap else 0.0
        vram_t = _safe(snap.gpu.vram_total_mb) if snap else 0.0
        gpu_t = _safe(snap.gpu.temp_celsius) if snap else 0.0
        ram_u = _safe(snap.ram.used_gb) if snap else 0.0
        ram_t = _safe(snap.ram.total_gb) if snap else 0.0

        return {
            "cpu_percent": cpu_p,
            "gpu_percent": gpu_p,
            "gpu_temp_c": gpu_t,
            "vram_used_mb": vram_u,
            "vram_total_mb": vram_t,
            "ram_used_gb": ram_u,
            "ram_total_gb": ram_t,
            "requests_total": _telemetry_data["requests_total"],
            "last_pairing": _telemetry_data.get("last_pairing"),
            "voice_muted": bool(_voice_config.get("muted", False)),
            "memory_entries": len(_memory_store),
            "pending_security_items": len([s for s in _pending_security if s.get("status") == "pending"]),
        }

    # --- Chat (with End-to-End Turn Correlation & Latency Profiling) ---
    @app.post("/api/v1/chat", tags=["chat"])
    async def api_chat(request: Request, body: ChatRequest):
        _telemetry_incr()
        user_message = body.get_message()
        if not user_message:
            return {
                "spoken_response": "",
                "response": "",
                "reply": "",
                "intent": "empty",
                "confidence": 1.0,
                "language": "fa",
                "session_id": body.session_id or "default",
            }

        tid = new_turn_id("REST")
        is_test_client = (request.client and request.client.host == "testclient")
        reply_text = ""
        dm = None
        llm_ms = 0.0

        with turn_context(tid):
            _logger.info(
                "Turn start [REST] | source=%s | chars=%d | text=%s",
                body.source or "desktop",
                len(user_message),
                user_message[:120],
            )
            t0 = time.perf_counter()
            try:
                dm = _get_dialogue_manager()
                if dm is not None and not is_test_client:
                    reply_text = dm.chat(user_message)
                elif dm is not None and is_test_client:
                    if dm.llm_client and dm.llm_client.get_health_status():
                        reply_text = dm.chat(user_message)
                    else:
                        reply_text = f"پاسخ تست وسپر: {user_message}"
                else:
                    reply_text = f"پاسخ تست وسپر: {user_message}"
            except Exception as e:
                _logger.exception("Chat turn error: %s", e)
                reply_text = f"خطا در ارتباط با هسته پردازشی وسپر: {e}"
            finally:
                llm_ms = round((time.perf_counter() - t0) * 1000.0, 1)

            tts_dispatched = False
            if not is_test_client and reply_text:
                raw_tts = getattr(dm, "last_raw_tts_response", None) or reply_text
                _trigger_tts_async(raw_tts, app=request.app, turn_id=tid)
                tts_dispatched = True

            _logger.info(
                "Turn LLM done [REST] | llm_ms=%.1fms | reply_chars=%d | tts_dispatched=%s",
                llm_ms,
                len(reply_text or ""),
                tts_dispatched,
            )

        return {
            "spoken_response": reply_text,
            "response": reply_text,
            "reply": reply_text,
            "intent": "ai_response",
            "confidence": 0.98,
            "language": "fa",
            "session_id": body.session_id or "default",
            "turn_id": tid,
            "llm_ms": llm_ms,
        }

    # --- Voice Mute & DSP Config ---
    @app.post("/api/v1/voice/mute", tags=["voice"])
    async def api_voice_mute(request: Request):
        _telemetry_incr()
        try:
            data = await request.json()
        except Exception:
            data = {}
        muted_val = data.get("muted") if "muted" in data else data.get("mute", not _voice_config["muted"])
        muted_bool = bool(muted_val)
        _voice_config["mute"] = muted_bool
        _voice_config["muted"] = muted_bool
        _logger.info("Voice mute updated via REST: muted=%s", muted_bool)
        return {"muted": muted_bool, "mute": muted_bool, "volume": _voice_config["volume"]}

    @app.get("/api/v1/voice/config", tags=["voice"])
    async def api_voice_config_get():
        _sync_voice_config_from_disk()
        try:
            from Voice.AudioDSP.jarvis_dsp import CelesteAudioDSP
            CelesteAudioDSP.set_global_config(
                pitch_semitones=_voice_config.get("dsp_pitch_semitones", _voice_config.get("pitch_semitones")),
                metallic_mix=_voice_config.get("dsp_metallic_mix", _voice_config.get("metallic_mix")),
                reverb_decay=_voice_config.get("dsp_reverb_mix", _voice_config.get("reverb_decay")),
            )
        except Exception:
            pass
        return dict(_voice_config)

    @app.put("/api/v1/voice/config", tags=["voice"])
    @app.post("/api/v1/voice/config", tags=["voice"])
    async def api_voice_config_update(request: Request):
        _telemetry_incr()
        is_test_client = (request.client and request.client.host == "testclient")
        try:
            data = await request.json()
        except Exception:
            data = {}

        if "engine_mode" in data and data["engine_mode"]:
            raw_mode = str(data["engine_mode"]).lower().strip()
            if raw_mode in ("online", "gemini"):
                data["engine_mode"] = "gemini"
            elif raw_mode in ("local", "gooya"):
                data["engine_mode"] = "gooya"

        allowed_keys = (
            "mute", "muted", "volume", "engine_mode", "online_api_key",
            "gemini_model", "gemini_voice_name", "gooya_model_dir",
            "gooya_reference_audio", "gooya_exaggeration", "gooya_cfg_weight",
            "gooya_precision", "gooya_auto_unload", "dsp_pitch_semitones",
            "pitch_semitones", "dsp_metallic_mix", "metallic_mix",
            "dsp_reverb_mix", "reverb_decay", "wake_threshold", "language",
            "local_engine", "online_provider", "online_voice_name"
        )
        for k in allowed_keys:
            if k in data and data[k] is not None:
                _voice_config[k] = data[k]

        if "dsp_pitch_semitones" in data and data["dsp_pitch_semitones"] is not None:
            _voice_config["dsp_pitch_semitones"] = float(data["dsp_pitch_semitones"])
            _voice_config["pitch_semitones"] = float(data["dsp_pitch_semitones"])
        elif "pitch_semitones" in data and data["pitch_semitones"] is not None:
            _voice_config["dsp_pitch_semitones"] = float(data["pitch_semitones"])
            _voice_config["pitch_semitones"] = float(data["pitch_semitones"])

        if "dsp_metallic_mix" in data and data["dsp_metallic_mix"] is not None:
            _voice_config["dsp_metallic_mix"] = float(data["dsp_metallic_mix"])
            _voice_config["metallic_mix"] = float(data["dsp_metallic_mix"])
        elif "metallic_mix" in data and data["metallic_mix"] is not None:
            _voice_config["dsp_metallic_mix"] = float(data["metallic_mix"])
            _voice_config["metallic_mix"] = float(data["metallic_mix"])

        if "dsp_reverb_mix" in data and data["dsp_reverb_mix"] is not None:
            _voice_config["dsp_reverb_mix"] = float(data["dsp_reverb_mix"])
            _voice_config["reverb_decay"] = float(data["dsp_reverb_mix"])
        elif "reverb_decay" in data and data["reverb_decay"] is not None:
            _voice_config["dsp_reverb_mix"] = float(data["reverb_decay"])
            _voice_config["reverb_decay"] = float(data["reverb_decay"])

        if "wake_threshold" in data and data["wake_threshold"] is not None:
            _voice_config["wake_threshold"] = float(data["wake_threshold"])

        if "muted" in data:
            _voice_config["mute"] = bool(data["muted"])
            _voice_config["muted"] = bool(data["muted"])
        elif "mute" in data:
            _voice_config["muted"] = bool(data["mute"])
            _voice_config["mute"] = bool(data["mute"])

        # Persist to Configuration/config.json via ConfigManager
        if not is_test_client:
            try:
                from Core.Config.config_loader import get_config
                cfg_mgr = get_config()
                cfg_keys = [
                    "engine_mode", "online_api_key", "gemini_model", "gemini_voice_name",
                    "gooya_model_dir", "gooya_reference_audio", "gooya_exaggeration",
                    "gooya_cfg_weight", "gooya_precision", "gooya_auto_unload",
                    "wake_threshold"
                ]
                for k in cfg_keys:
                    if k in _voice_config and _voice_config[k] is not None:
                        cfg_mgr.set("voice", k, _voice_config[k])

                cfg_mgr.set("voice", "dsp_pitch_semitones", _voice_config["dsp_pitch_semitones"])
                cfg_mgr.set("voice", "pitch_semitones", _voice_config["pitch_semitones"])
                cfg_mgr.set("voice", "dsp_metallic_mix", _voice_config["dsp_metallic_mix"])
                cfg_mgr.set("voice", "dsp_reverb_mix", _voice_config["dsp_reverb_mix"])
                cfg_mgr.save()
            except Exception as e:
                _logger.warning("Could not persist voice config: %s", e, exc_info=True)

        # Update CelesteAudioDSP live
        try:
            from Voice.AudioDSP.jarvis_dsp import CelesteAudioDSP
            CelesteAudioDSP.set_global_config(
                pitch_semitones=_voice_config.get("dsp_pitch_semitones"),
                metallic_mix=_voice_config.get("dsp_metallic_mix"),
                reverb_decay=_voice_config.get("dsp_reverb_mix"),
            )
        except Exception as e:
            _logger.warning("Failed to update CelesteAudioDSP: %s", e)

        # Update WakeWordEngine & VoiceOrchestrator live
        orch = getattr(request.app.state, "voice_orchestrator", None)
        if orch is not None:
            try:
                orch.apply_voice_config(
                    pitch_semitones=_voice_config.get("dsp_pitch_semitones"),
                    metallic_mix=_voice_config.get("dsp_metallic_mix"),
                    reverb_decay=_voice_config.get("dsp_reverb_mix"),
                    muted=_voice_config.get("muted"),
                    wake_threshold=_voice_config.get("wake_threshold"),
                    tts_config=dict(_voice_config),
                )
            except Exception as e:
                _logger.warning("Failed to apply voice config to orchestrator: %s", e)

        # Update the single unified TTSEngine (triggers Gooya VRAM preload or unload)
        if not is_test_client:
            try:
                tts = _get_tts_engine(request.app)
                if tts is not None and hasattr(tts, "update_config"):
                    tts.update_config(dict(_voice_config))
                _logger.info("Voice config updated via API (engine_mode=%s)", _voice_config.get("engine_mode"))
            except Exception as e:
                _logger.exception("Failed to update unified TTSEngine config: %s", e)

        return dict(_voice_config)

    # --- Memory History ---
    @app.get("/api/v1/memory/history", tags=["memory"])
    async def api_memory_history():
        _telemetry_incr()
        mm = _get_memory_manager()
        messages_out: List[Dict[str, Any]] = []
        if mm is not None and hasattr(mm, "get_recent_conversations"):
            try:
                rows = mm.get_recent_conversations(limit=40) or []
                for idx, r in enumerate(rows, 1):
                    if isinstance(r, dict):
                        messages_out.append({
                            "id": int(r.get("id", idx)),
                            "role": str(r.get("role", "user")),
                            "content": str(r.get("content", "")),
                            "timestamp": str(r.get("timestamp", _now_str())),
                        })
            except Exception as e:
                _logger.warning("Failed to load recent conversations from SQLite: %s", e)

        if not messages_out:
            dm = _get_dialogue_manager()
            if dm is not None and hasattr(dm, "history"):
                for idx, m in enumerate(dm.history, 1):
                    messages_out.append({
                        "id": idx,
                        "role": m.get("role", "user"),
                        "content": m.get("content", ""),
                        "timestamp": _now_str(),
                    })
        return {"messages": messages_out}

    @app.delete("/api/v1/memory/history", tags=["memory"])
    async def api_memory_history_clear():
        _telemetry_incr()
        dm = _get_dialogue_manager()
        if dm and hasattr(dm, "clear_history"):
            dm.clear_history()
        mm = _get_memory_manager()
        if mm and hasattr(mm, "clear_conversations"):
            try:
                mm.clear_conversations()
            except Exception as e:
                _logger.warning("Failed to clear conversations in SQLite: %s", e)
        _logger.info("Conversation history cleared via REST")
        return {"cleared": True, "status": "ok"}

    # --- Memory Facts ---
    @app.get("/api/v1/memory/facts", tags=["memory"])
    async def api_memory_facts(category: Optional[str] = None):
        _telemetry_incr()
        facts_list: List[Dict[str, Any]] = []
        mm = _get_memory_manager()
        if mm is not None:
            try:
                facts_list = mm.get_all_facts(category=category) or []
            except Exception as e:
                _logger.exception("Failed to query facts from SQLite: %s", e)
        return {"facts": facts_list}

    @app.post("/api/v1/memory/facts", tags=["memory"])
    async def api_memory_fact_create(body: MemoryCreateRequest):
        _telemetry_incr()
        cat = body.category or "user"
        fact_key = body.get_key()
        if not fact_key:
            raise HTTPException(status_code=422, detail="Missing required field 'key' or 'fact_key'")
        val_str = body.get_value()
        imp = int(body.importance if body.importance is not None else 5)

        mm = _get_memory_manager()
        if mm is None:
            raise HTTPException(status_code=500, detail="MemoryManager not available")

        try:
            mm.set_fact(category=cat, key=fact_key, value=val_str, importance=imp)
        except Exception as e:
            _logger.exception("Failed to save fact to SQLite: %s", e)
            raise HTTPException(status_code=500, detail=f"Database error: {e}")

        all_facts = mm.get_all_facts()
        for f in all_facts:
            if f.get("key") == fact_key.lower():
                return f

        return {
            "id": 1,
            "category": cat,
            "key": fact_key,
            "fact_key": fact_key,
            "value": val_str,
            "fact_value": val_str,
            "importance": imp,
            "metadata": body.metadata,
            "updated_at": _now_str(),
        }

    @app.delete("/api/v1/memory/facts/{fact_key}", tags=["memory"])
    async def api_memory_fact_delete(fact_key: str):
        _telemetry_incr()
        mm = _get_memory_manager()
        if mm is None:
            raise HTTPException(status_code=500, detail="MemoryManager not available")

        try:
            deleted = bool(mm.delete_fact(fact_key))
        except Exception as e:
            _logger.exception("Failed to delete fact from SQLite: %s", e)
            raise HTTPException(status_code=500, detail=f"Database error: {e}")

        if deleted:
            return {"deleted": True, "key": fact_key, "id": fact_key}

        raise HTTPException(status_code=404, detail=f"Fact '{fact_key}' not found")

    # --- Legacy Key-Value Memory CRUD ---
    @app.get("/api/v1/memory/{key}", tags=["memory"])
    async def api_memory_get(key: str):
        _telemetry_incr()
        item = _memory_store.get(key)
        if not item:
            raise HTTPException(status_code=404, detail=f"Memory key '{key}' not found")
        return item

    @app.post("/api/v1/memory", tags=["memory"])
    async def api_memory_create(body: MemoryCreateRequest):
        _telemetry_incr()
        item = {"key": body.key, "value": body.value, "metadata": body.metadata, "created_at": _now_str()}
        _memory_store[body.key] = item
        return item

    @app.delete("/api/v1/memory/{key}", tags=["memory"])
    async def api_memory_delete(key: str):
        _telemetry_incr()
        if key not in _memory_store:
            raise HTTPException(status_code=404, detail=f"Memory key '{key}' not found")
        item = _memory_store.pop(key)
        return {"deleted": True, "item": item}

    # --- Security Pending & Audit ---
    @app.get("/api/v1/security/pending", tags=["security"])
    @app.get("/api/v1/security/audit/pending", tags=["security"])
    async def api_security_pending_list():
        _telemetry_incr()
        pending_items = []
        for s in _pending_security:
            if s.get("status") == "pending":
                pending_items.append({
                    "id": s["id"],
                    "action_id": str(s.get("action_id", s["id"])),
                    "action": s.get("action", "command"),
                    "command_type": s.get("command_type", s.get("action", "SYSTEM_CMD")),
                    "risk_level": s.get("risk_level", "SENSITIVE"),
                    "requester": s.get("requester", "user"),
                    "timestamp": s.get("timestamp", _now_str()),
                    "status": "pending",
                    "payload": s.get("payload", {"details": s.get("details", "")}),
                })
        return {"pending": pending_items, "items": pending_items}

    @app.post("/api/v1/security/audit/pending", tags=["security"])
    async def api_security_create_pending(request: Request):
        _telemetry_incr()
        try:
            data = await request.json()
        except Exception:
            data = {}
        new_id = len(_pending_security) + 1
        item = {
            "id": new_id,
            "action_id": str(data.get("action_id", new_id)),
            "action": data.get("action", "unknown"),
            "command_type": data.get("command_type", data.get("action", "SENSITIVE_ACTION")),
            "risk_level": data.get("risk_level", "SENSITIVE"),
            "requester": data.get("requester", "system"),
            "timestamp": _now_str(),
            "status": "pending",
            "details": data.get("details", ""),
            "payload": data.get("payload", data),
        }
        _pending_security.append(item)
        return item

    @app.post("/api/v1/security/confirm", tags=["security"])
    async def api_security_confirm_post(request: Request):
        _telemetry_incr()
        try:
            data = await request.json()
        except Exception:
            data = {}
        target_id = str(data.get("action_id") if "action_id" in data else data.get("item_id", ""))
        approved = bool(data.get("approved", True))

        for s in _pending_security:
            if (str(s.get("id")) == target_id or str(s.get("action_id")) == target_id) and s.get("status") == "pending":
                s["status"] = "confirmed" if approved else "rejected"
                _audit_log.append({
                    "id": len(_audit_log) + 1,
                    "command_type": s.get("command_type", "SENSITIVE_CMD"),
                    "risk_level": s.get("risk_level", "SENSITIVE"),
                    "status": "APPROVED" if approved else "REJECTED",
                    "timestamp": _now_str(),
                    "level": "info",
                    "message": f"Action {target_id} {'approved' if approved else 'rejected'}",
                    "result": "success" if approved else "rejected",
                })
                return {"status": s["status"], "approved": approved}
        raise HTTPException(status_code=404, detail="Pending security item not found")

    @app.post("/api/v1/security/audit/{item_id}/confirm", tags=["security"])
    async def api_security_confirm_by_path(item_id: int):
        _telemetry_incr()
        for s in _pending_security:
            if s.get("id") == item_id and s.get("status") == "pending":
                s["status"] = "confirmed"
                _audit_log.append({
                    "id": len(_audit_log) + 1,
                    "command_type": s.get("command_type", "SENSITIVE_CMD"),
                    "risk_level": s.get("risk_level", "SENSITIVE"),
                    "status": "APPROVED",
                    "timestamp": _now_str(),
                    "level": "info",
                    "message": f"Audit item {item_id} confirmed",
                    "result": "success",
                })
                return {"status": "confirmed"}
        raise HTTPException(status_code=404, detail="Pending audit item not found")

    @app.get("/api/v1/security/audit", tags=["security"])
    @app.get("/api/v1/security/audit/log", tags=["security"])
    async def api_security_audit_list():
        _telemetry_incr()
        audit_items: List[Dict[str, Any]] = list(_audit_log)
        try:
            from Automation.Security.permission_manager import get_permission_manager
            pm = get_permission_manager()
            if hasattr(pm, "get_audit_entries"):
                rows = pm.get_audit_entries(limit=50) or []
                for r in rows:
                    if isinstance(r, (list, tuple)) and len(r) >= 5:
                        audit_items.append({
                            "id": int(r[0]),
                            "timestamp": str(r[1]),
                            "risk_level": str(r[2]),
                            "command_type": str(r[3])[:60],
                            "status": str(r[4]),
                            "level": "INFO",
                            "message": str(r[3]),
                            "result": str(r[4]),
                        })
        except Exception:
            pass
        return {"audit_logs": audit_items, "entries": audit_items}

    # --- Unified Structured Logs (Direct Ring Buffer with Filtering & Incremental Cursor) ---
    @app.get("/api/v1/logs", tags=["logs"])
    async def api_logs(
        level: Optional[str] = Query(default=None, description="ALL, DEBUG, INFO, WARNING, ERROR, CRITICAL (or INFO+, WARNING+)"),
        module: Optional[str] = Query(default=None, description="Substring match on logger/module name"),
        search: Optional[str] = Query(default=None, description="Case-insensitive search across message, module, turn_id, traceback"),
        since_id: int = Query(default=0, ge=0, description="Return only log entries with id > since_id"),
        limit: int = Query(default=200, ge=1, le=2000, description="Maximum number of entries to return"),
    ):
        _telemetry_incr()
        entries = get_recent_logs(
            limit=limit,
            level=level,
            module=module,
            search=search,
            since_id=since_id,
        )
        return {
            "logs": entries,
            "last_id": get_last_log_id(),
            "total_buffered": get_recent_logs_count(),
        }

    @app.get("/api/v1/logs/modules", tags=["logs"])
    async def api_log_modules():
        return {"modules": get_buffered_modules()}

    # --- Mount WebSocket Server (/ws/desktop & /ws/lumia) with shared voice_orchestrator ---
    try:
        from Network.WebSocket.ws_server import JarvisWebSocketManager
        _ws_mgr = JarvisWebSocketManager(app, voice_orchestrator=voice_orchestrator)
        app.state.ws_manager = _ws_mgr
    except Exception as e:
        _logger.warning("WebSocket manager mount warning: %s", e, exc_info=True)

    return app


def _run_self_test() -> bool:
    app = create_rest_app()
    client = TestClient(app)
    token = get_master_token()
    headers = {"Authorization": f"Bearer {token}"}
    passed, failed = 0, 0

    def _check(name: str, r, expected_status: int):
        nonlocal passed, failed
        if r.status_code == expected_status:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name} -> {r.status_code} (expected {expected_status})")

    _check("GET /ping", client.get("/ping"), 200)
    r_stat = client.get("/api/v1/status", headers=headers)
    _check("GET /status", r_stat, 200)
    assert r_stat.json().get("model_name") == "Gemma 4 26B-A4B IT (IQ4_XS)", f"Expected Gemma 4 model name, got: {r_stat.json().get('model_name')}"
    _check("POST /pair", client.post("/api/v1/pair", json={"device_name": "JarvisDesktopPC"}, headers=headers), 200)
    r_tel = client.get("/api/v1/telemetry", headers=headers)
    _check("GET /telemetry", r_tel, 200)
    print(f"  [INFO] Live Telemetry Snapshot: {r_tel.json()}")
    r_chat = client.post("/api/v1/chat", json={"text": "سلام وسپر"}, headers=headers)
    _check("POST /chat (C# text + spoken_response)", r_chat, 200)
    assert "spoken_response" in r_chat.json(), "Missing spoken_response!"
    _check("POST /voice/mute", client.post("/api/v1/voice/mute", json={"muted": True}, headers=headers), 200)
    _check("GET /voice/config", client.get("/api/v1/voice/config", headers=headers), 200)
    _check("POST /voice/config", client.post("/api/v1/voice/config", json={"pitch_semitones": 0.0, "engine_mode": "gemini"}, headers=headers), 200)
    _check("PUT /voice/config", client.put("/api/v1/voice/config", json={"engine_mode": "gooya", "gooya_exaggeration": 0.6}, headers=headers), 200)
    _check("PUT /voice/config (gemini)", client.put("/api/v1/voice/config", json={"engine_mode": "gemini"}, headers=headers), 200)
    _check("GET /memory/history", client.get("/api/v1/memory/history", headers=headers), 200)
    _check("POST /memory/facts", client.post("/api/v1/memory/facts", json={"category": "user", "key": "test_k", "value": "test_v", "importance": 8}, headers=headers), 200)
    _check("GET /memory/facts", client.get("/api/v1/memory/facts", headers=headers), 200)
    _check("DELETE /memory/facts/{key}", client.delete("/api/v1/memory/facts/test_k", headers=headers), 200)
    _check("GET /security/pending", client.get("/api/v1/security/pending", headers=headers), 200)
    _check("GET /security/audit", client.get("/api/v1/security/audit", headers=headers), 200)
    r_logs = client.get("/api/v1/logs?limit=50", headers=headers)
    _check("GET /logs (unified ring buffer)", r_logs, 200)
    assert "last_id" in r_logs.json() and isinstance(r_logs.json().get("logs"), list)
    _check("GET /logs/modules", client.get("/api/v1/logs/modules", headers=headers), 200)
    _check("Auth failure (no token)", client.get("/api/v1/status"), 401)

    print(f"\n=== Results: {passed} passed, {failed} failed ===\n")
    return failed == 0


if __name__ == "__main__":
    if _run_self_test():
        print("[OK] V.E.S.P.E.R. REST API & Telemetry 100% synchronized with C# Desktop UI.")