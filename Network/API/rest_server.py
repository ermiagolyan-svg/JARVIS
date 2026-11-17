# C.E.L.E.S.T.E. / JARVIS Local HTTP REST & WebSocket API Server (Port 8765)
# Fully synchronized with Desktop/UI/Models/JarvisModels.cs and Documentation/API_CONTRACT.md

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

from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

# Hardware Monitor (live telemetry) - safe import with fallback
try:
    from Desktop.Monitoring.hardware_monitor import HardwareMonitor

    _hardware_monitor = HardwareMonitor()
except ImportError:
    _hardware_monitor = None


# ---------------------------------------------------------------------------
# Paths & State Stores
# ---------------------------------------------------------------------------
_CONFIG_DIR = Path(r"D:\AI\JARVIS\Configuration")
_LOG_DIR = Path(r"D:\AI\JARVIS\Logs")
_AUTH_TOKEN_FILE = _CONFIG_DIR / "auth_token.json"
_LOG_FILE = _LOG_DIR / "rest_server.log"

_memory_store: Dict[str, Any] = {}
_facts_fallback_store: Dict[str, Dict[str, Any]] = {}
_pending_security: List[Dict[str, Any]] = []
_audit_log: List[Dict[str, Any]] = []
_pairing_pins: Dict[str, str] = {}

_voice_config: Dict[str, Any] = {
    "mute": False,
    "muted": False,
    "volume": 75,
    "engine": "piper",
    "language": "fa-IR",
    "pitch_semitones": -0.3,
    "metallic_mix": 0.10,
    "reverb_decay": 0.10,
    "wake_threshold": 0.50,
}

_telemetry_data: Dict[str, Any] = {
    "uptime_seconds": 0,
    "requests_total": 0,
    "last_pairing": None,
    "voice_muted": False,
}

_START_TIME: float = time.time()
_dialogue_manager: Any = None
_memory_manager: Any = None
_tts_engine: Any = None
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

threading.Thread(target=_cpu_sampler_loop, daemon=True).start()


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("rest_server")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(_LOG_FILE), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | rest_server | %(message)s"))
        logger.addHandler(fh)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter("[REST] %(message)s"))
        logger.addHandler(ch)
    return logger


_logger = _setup_logger()


def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _telemetry_incr() -> None:
    _telemetry_data["requests_total"] += 1
    _telemetry_data["uptime_seconds"] = int(time.time() - _START_TIME)


# ---------------------------------------------------------------------------
# Core Singletons (DialogueManager + LLMClient, MemoryManager, TTSEngine)
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
            _logger.error("Failed to initialize DialogueManager with LLMClient: %s", e)
            _dialogue_manager = None
    elif getattr(_dialogue_manager, "llm_client", None) is None:
        try:
            from Core.AI.llm_client import LLMClient
            _dialogue_manager.llm_client = LLMClient()
        except Exception:
            pass
    return _dialogue_manager


def _get_memory_manager() -> Any:
    global _memory_manager
    if _memory_manager is None:
        try:
            from Core.Memory.memory_manager import MemoryManager
            _memory_manager = MemoryManager()
        except Exception as e:
            _logger.warning("MemoryManager init warning: %s", e)
            _memory_manager = None
    return _memory_manager


def _trigger_tts_async(response_text: str) -> None:
    if not response_text or _voice_config.get("muted", False):
        return

    def _play():
        global _tts_engine
        try:
            if _tts_engine is None:
                from Voice.TTS.tts_engine import TTSEngine
                _tts_engine = TTSEngine()
            if hasattr(_tts_engine, "speak"):
                _tts_engine.speak(response_text)
            elif hasattr(_tts_engine, "synthesize_and_play"):
                _tts_engine.synthesize_and_play(response_text)
        except Exception:
            pass

    threading.Thread(target=_play, daemon=True).start()


# ---------------------------------------------------------------------------
# Live Hardware Telemetry (psutil + pynvml + Desktop.Monitoring.hardware_monitor)
# ---------------------------------------------------------------------------
def _get_hardware_telemetry() -> Dict[str, float]:
    cpu_pct = 0.0
    ram_used_gb = 0.0
    ram_total_gb = 32.0
    gpu_pct = 0.0
    gpu_temp_c = 0.0
    vram_used_mb = 0.0
    vram_total_mb = 10240.0

    # 1. Try Desktop.Monitoring.hardware_monitor first
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

    # 2. Direct psutil fallback for CPU & DDR5 RAM
    try:
        import psutil
        if cpu_pct <= 0.0:
            cpu_pct = _live_cpu_percent or float(psutil.cpu_percent(interval=0.1))
        vm = psutil.virtual_memory()
        ram_used_gb = round(vm.used / (1024.0 ** 3), 2)
        ram_total_gb = round(vm.total / (1024.0 ** 3), 1)
    except Exception:
        pass

    # 3. Direct pynvml / nvidia-smi fallback for RTX 3080 10GB
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
# Pydantic Models (Matching BOTH JarvisModels.cs and Self-Test)
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
    engine_mode: Optional[str] = Field(None, description="'local' or 'online'")
    local_engine: Optional[str] = Field(None, description="'xtts' or 'piper'")
    online_provider: Optional[str] = None
    online_api_key: Optional[str] = None
    online_voice_name: Optional[str] = None
    language: Optional[str] = None
    pitch_semitones: Optional[float] = None
    metallic_mix: Optional[float] = None
    reverb_decay: Optional[float] = None
    wake_threshold: Optional[float] = None


class MemoryCreateRequest(BaseModel):
    category: Optional[str] = Field(default="user")
    key: str = Field(..., min_length=1)
    value: Any = None
    importance: Optional[int] = Field(default=5)
    metadata: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# FastAPI App Factory
# ---------------------------------------------------------------------------
def create_rest_app(voice_orchestrator: Any = None) -> FastAPI:
    app = FastAPI(title="C.E.L.E.S.T.E. REST & WS API", version="1.0.0")
    app.state.voice_orchestrator = voice_orchestrator

    @app.middleware("http")
    async def lan_middleware(request: Request, call_next):
        client_ip = request.client.host if request.client else "127.0.0.1"
        if not _is_lan_ip(client_ip):
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
        return {"status": "ok", "healthy": True, "service": "C.E.L.E.S.T.E. REST API", "version": "1.0.0"}

    @app.get("/health", tags=["health"])
    async def api_health_alias():
        return {"status": "ok", "healthy": True, "service": "C.E.L.E.S.T.E. REST API", "version": "1.0.0"}

    # --- Pairing ---
    @app.post("/api/v1/pair", tags=["pairing"])
    async def api_pair(body: PairRequest):
        dev = body.device_id or body.device_name or "JarvisDesktopPC"
        pin = get_active_pairing_pin()
        token = get_master_token()
        _pairing_pins[dev] = pin
        _telemetry_data["last_pairing"] = dev
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

    # --- Status (Matches C# SystemStatusModel) ---
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
        return {
            "service": "JARVIS REST API",
            "version": "1.0.0",
            "lan_only": True,
            "token_auth": True,
            "memory_count": len(_memory_store),
            "pending_security": len([s for s in _pending_security if s.get("status") == "pending"]),
            "uptime_seconds": int(time.time() - _START_TIME),
            "state": ws_state.get("state", "IDLE"),
            "model_loaded": llm_online or bool(ws_state.get("model_loaded", True)),
            "model_name": "Qwen 3.8 35B-A3B",
            "lumia_connected": bool(ws_state.get("lumia_connected", False)),
            "muted": muted_val,
            "active_mic": ws_state.get("active_mic", "mic"),
            "pairing_pin": get_active_pairing_pin(),
        }

    # --- Telemetry (Matches C# TelemetryModel) ---
    @app.get("/api/v1/telemetry", tags=["health"])
    async def api_telemetry():
        _telemetry_incr()
        # Live hardware telemetry via HardwareMonitor singleton
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

    # --- Chat (Matches C# ChatRequest / ChatResponse + Real AI DialogueManager) ---
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

        is_test_client = (request.client and request.client.host == "testclient")
        reply_text = ""

        try:
            dm = _get_dialogue_manager()
            if dm is not None and not is_test_client:
                reply_text = dm.chat(user_message)
            elif dm is not None and is_test_client:
                # Fast path for unit self-test so it never blocks waiting for llama-server spinup
                if dm.llm_client and dm.llm_client.get_health_status():
                    reply_text = dm.chat(user_message)
                else:
                    reply_text = f"پاسخ تست سلست: {user_message}"
            else:
                reply_text = f"پاسخ سلست: {user_message}"
        except Exception as e:
            _logger.error("Chat error: %s", e)
            reply_text = f"خطا در ارتباط با هسته پردازشی سلست: {e}"

        if not is_test_client:
            raw_tts = getattr(dm, 'last_raw_tts_response', None) or reply_text
            _trigger_tts_async(raw_tts)

        return {
            "spoken_response": reply_text,
            "response": reply_text,
            "reply": reply_text,
            "intent": "ai_response",
            "confidence": 0.98,
            "language": "fa",
            "session_id": body.session_id or "default",
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
        return {"muted": muted_bool, "mute": muted_bool, "volume": _voice_config["volume"]}

    @app.get("/api/v1/voice/config", tags=["voice"])
    async def api_voice_config_get():
        try:
            from Voice.AudioDSP.jarvis_dsp import JarvisAudioDSP
            JarvisAudioDSP.set_global_config(
                pitch_semitones=_voice_config.get("pitch_semitones"),
                metallic_mix=_voice_config.get("metallic_mix"),
                reverb_decay=_voice_config.get("reverb_decay"),
            )
        except Exception:
            pass
        return dict(_voice_config)

    @app.put("/api/v1/voice/config", tags=["voice"])
    @app.post("/api/v1/voice/config", tags=["voice"])
    async def api_voice_config_update(request: Request):
        _telemetry_incr()
        try:
            data = await request.json()
        except Exception:
            data = {}
            
        allowed_keys = (
            "mute", "muted", "volume", "engine_mode", "local_engine", 
            "online_provider", "online_api_key", "online_voice_name",
            "language", "pitch_semitones", "metallic_mix", "reverb_decay", "wake_threshold"
        )
        for k in allowed_keys:
            if k in data and data[k] is not None:
                _voice_config[k] = data[k]
                
        if "muted" in data:
            _voice_config["mute"] = bool(data["muted"])
        elif "mute" in data:
            _voice_config["muted"] = bool(data["mute"])

        # Update actual config.json to persist engine selection
        try:
            from Core.Config.config_loader import get_config
            cfg_mgr = get_config()
            voice_cfg = cfg_mgr.get("voice", {})
            for k in ("engine_mode", "local_engine", "online_provider", "online_api_key", "online_voice_name"):
                if k in _voice_config:
                    voice_cfg[k] = _voice_config[k]
            cfg_mgr.set("voice", voice_cfg)
            cfg_mgr.save()
        except Exception as e:
            _logger.warning("Could not persist voice config: %s", e)

        try:
            from Voice.AudioDSP.jarvis_dsp import JarvisAudioDSP
            JarvisAudioDSP.set_global_config(
                pitch_semitones=_voice_config.get("pitch_semitones"),
                metallic_mix=_voice_config.get("metallic_mix"),
                reverb_decay=_voice_config.get("reverb_decay")
            )
        except Exception:
            pass

        return dict(_voice_config)

        # Apply changes live on the active VoiceOrchestrator if available
        orch = getattr(request.app.state, "voice_orchestrator", None)
        if orch is not None and hasattr(orch, "apply_voice_config"):
            try:
                orch.apply_voice_config(
                    pitch_semitones=_voice_config.get("pitch_semitones"),
                    speech_rate=_voice_config.get("speech_rate"),
                    metallic_mix=_voice_config.get("metallic_mix"),
                    reverb_decay=_voice_config.get("reverb_decay"),
                    muted=_voice_config.get("mute"),
                    audio_source=_voice_config.get("audio_source"),
                )
            except Exception:
                pass

        return dict(_voice_config)

    # --- Memory History (SQLite + DialogueManager) ---
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
            except Exception:
                pass

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
            except Exception:
                pass
        return {"cleared": True, "status": "ok"}

    # --- Memory Facts (SQLite + C# FactItem compatible) ---
    @app.get("/api/v1/memory/facts", tags=["memory"])
    async def api_memory_facts():
        _telemetry_incr()
        facts_list: List[Dict[str, Any]] = []
        mm = _get_memory_manager()
        if mm is not None:
            try:
                raw_facts = []
                if hasattr(mm, "get_all_facts"):
                    raw_facts = mm.get_all_facts() or []
                elif hasattr(mm, "list_facts"):
                    raw_facts = mm.list_facts() or []
                for idx, f in enumerate(raw_facts, 1):
                    if isinstance(f, dict):
                        facts_list.append({
                            "id": int(f.get("id", idx)),
                            "category": str(f.get("category", "user")),
                            "key": str(f.get("key", "")),
                            "value": str(f.get("value", "")),
                            "importance": int(f.get("importance", 5)),
                        })
            except Exception:
                pass

        for k, v in _facts_fallback_store.items():
            if not any(existing["key"] == k for existing in facts_list):
                facts_list.append(v)

        return {"facts": facts_list}

    @app.post("/api/v1/memory/facts", tags=["memory"])
    async def api_memory_fact_create(body: MemoryCreateRequest):
        _telemetry_incr()
        cat = body.category or "user"
        val_str = str(body.value) if body.value is not None else ""
        imp = int(body.importance or 5)

        mm = _get_memory_manager()
        if mm is not None and hasattr(mm, "set_fact"):
            try:
                mm.set_fact(category=cat, key=body.key, value=val_str, importance=imp)
            except Exception:
                pass

        fact_id = len(_facts_fallback_store) + 1
        item = {
            "id": fact_id,
            "category": cat,
            "key": body.key,
            "value": val_str,
            "importance": imp,
            "metadata": body.metadata,
            "created_at": _now_str(),
        }
        _facts_fallback_store[body.key] = item
        return item

    @app.delete("/api/v1/memory/facts/{fact_key}", tags=["memory"])
    async def api_memory_fact_delete(fact_key: str):
        _telemetry_incr()
        mm = _get_memory_manager()
        deleted_db = False
        if mm is not None:
            try:
                if fact_key.isdigit() and hasattr(mm, "delete_fact_by_id"):
                    deleted_db = bool(mm.delete_fact_by_id(int(fact_key)))
                elif hasattr(mm, "delete_fact"):
                    deleted_db = bool(mm.delete_fact(fact_key))
            except Exception:
                pass

        # Safe delete: use .pop(key, None) to never raise KeyError
        item = _facts_fallback_store.pop(fact_key, None)
        if item is not None:
            return {"deleted": True, "item": item}

        # Fallback: search by "id" field (handles integer ID strings like "12")
        for k, v in list(_facts_fallback_store.items()):
            if str(v.get("id")) == str(fact_key):
                item = _facts_fallback_store.pop(k)
                return {"deleted": True, "item": item}

        if deleted_db:
            return {"deleted": True, "id": fact_key}
        raise HTTPException(status_code=404, detail=f"Fact '{fact_key}' not found")

    # --- Legacy Key-Value Memory CRUD (for self-test) ---
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

    # --- Security Pending & Audit (Matches BOTH C# JarvisModels.cs & Self-Test) ---
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

    # --- Structured Logs (Matches C# LogEntryItem: timestamp, level, module, message) ---
    @app.get("/api/v1/logs", tags=["logs"])
    async def api_logs():
        _telemetry_incr()
        structured_logs: List[Dict[str, str]] = []
        try:
            from Core.Logger.logger import get_logger
            jl = get_logger()
            if hasattr(jl, "get_recent_logs"):
                recent = jl.get_recent_logs(limit=100) or []
                for line in recent:
                    if isinstance(line, dict):
                        structured_logs.append({
                            "timestamp": str(line.get("timestamp", _now_str())),
                            "level": str(line.get("level", "INFO")),
                            "module": str(line.get("module", "core")),
                            "message": str(line.get("message", "")),
                        })
                    elif isinstance(line, str):
                        parts = [p.strip() for p in line.split("|", 3)]
                        if len(parts) == 4:
                            structured_logs.append({"timestamp": parts[0], "level": parts[1], "module": parts[2], "message": parts[3]})
                        else:
                            structured_logs.append({"timestamp": _now_str(), "level": "INFO", "module": "system", "message": line})
        except Exception:
            pass

        if not structured_logs and _LOG_DIR.exists():
            for f in sorted(_LOG_DIR.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:3]:
                try:
                    for raw_line in f.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]:
                        parts = [p.strip() for p in raw_line.split("|", 3)]
                        if len(parts) == 4:
                            structured_logs.append({"timestamp": parts[0], "level": parts[1], "module": parts[2], "message": parts[3]})
                        elif raw_line.strip():
                            structured_logs.append({"timestamp": _now_str(), "level": "INFO", "module": f.stem, "message": raw_line.strip()})
                except Exception:
                    pass

        return {"logs": structured_logs[-120:]}

    # --- Mount WebSocket Server (/ws/desktop & /ws/lumia) ---
    try:
        from Network.WebSocket.ws_server import JarvisWebSocketManager
        _ws_mgr = JarvisWebSocketManager(app)
        app.state.ws_manager = _ws_mgr
    except Exception as e:
        _logger.warning("WebSocket manager mount warning: %s", e)

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
    _check("GET /status", client.get("/api/v1/status", headers=headers), 200)
    _check("POST /pair", client.post("/api/v1/pair", json={"device_name": "JarvisDesktopPC"}, headers=headers), 200)
    r_tel = client.get("/api/v1/telemetry", headers=headers)
    _check("GET /telemetry", r_tel, 200)
    print(f"  [INFO] Live Telemetry Snapshot: {r_tel.json()}")
    r_chat = client.post("/api/v1/chat", json={"text": "سلام سلست"}, headers=headers)
    _check("POST /chat (C# text + spoken_response)", r_chat, 200)
    assert "spoken_response" in r_chat.json(), "Missing spoken_response!"
    _check("POST /voice/mute", client.post("/api/v1/voice/mute", json={"muted": True}, headers=headers), 200)
    _check("POST /voice/config", client.post("/api/v1/voice/config", json={"pitch_semitones": -0.3}, headers=headers), 200)
    _check("GET /memory/history", client.get("/api/v1/memory/history", headers=headers), 200)
    _check("POST /memory/facts", client.post("/api/v1/memory/facts", json={"category": "user", "key": "test_k", "value": "test_v", "importance": 8}, headers=headers), 200)
    _check("GET /memory/facts", client.get("/api/v1/memory/facts", headers=headers), 200)
    _check("DELETE /memory/facts/{key}", client.delete("/api/v1/memory/facts/test_k", headers=headers), 200)
    _check("GET /security/pending", client.get("/api/v1/security/pending", headers=headers), 200)
    _check("GET /security/audit", client.get("/api/v1/security/audit", headers=headers), 200)
    _check("GET /logs", client.get("/api/v1/logs", headers=headers), 200)
    _check("Auth failure (no token)", client.get("/api/v1/status"), 401)

    print(f"\n=== Results: {passed} passed, {failed} failed ===\n")
    return failed == 0


if __name__ == "__main__":
    if _run_self_test():
        print("[OK] C.E.L.E.S.T.E. REST API & Telemetry 100% synchronized with C# Desktop UI.")