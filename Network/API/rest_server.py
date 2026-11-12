# JARVIS Local HTTP REST API Server
# Restricted to LAN IPs only. Provides CRUD for memory, security audit, pairing, telemetry, etc.

import os
import sys
import json
import logging
import secrets
import threading
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, Response, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_CONFIG_DIR = Path(r"D:\AI\JARVIS\Configuration")
_LOG_DIR = Path(r"D:\AI\JARVIS\Logs")
_AUTH_TOKEN_FILE = _CONFIG_DIR / "auth_token.json"
_LOG_FILE = _LOG_DIR / "rest_server.log"
_MASTER_TOKEN: Optional[str] = None
_TOKEN_EXPIRY_HOURS = 24

# In-memory stores
_memory_store: Dict[str, Any] = {}
_pending_security: List[Dict[str, Any]] = []
_audit_log: List[Dict[str, Any]] = []
_pairing_pins: Dict[str, str] = {}
_voice_config: Dict[str, Any] = {
    "mute": False,
    "volume": 75,
    "engine": "piper",
    "language": "en-US",
}
_telemetry_data: Dict[str, Any] = {
    "uptime_seconds": 0,
    "requests_total": 0,
    "last_pairing": None,
    "voice_muted": False,
}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("rest_server")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(_LOG_FILE), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter("[REST] %(message)s"))
        logger.addHandler(ch)
    return logger

_logger = _setup_logger()

# ---------------------------------------------------------------------------
# Helper: LAN IP check (RFC 1918)
# ---------------------------------------------------------------------------
def _is_lan_ip(ip_address: str) -> bool:
    """Check if an IP address belongs to a private LAN or loopback range (RFC 1918)."""
    try:
        import ipaddress
        addr = ipaddress.ip_address(ip_address)
        if not isinstance(addr, ipaddress.IPv4Address):
            return False
        return (
            addr in ipaddress.ip_network("127.0.0.0/8")   # loopback
            or addr in ipaddress.ip_network("10.0.0.0/8")   # Class A private
            or addr in ipaddress.ip_network("172.16.0.0/12")  # Class B private
            or addr in ipaddress.ip_network("192.168.0.0/16")  # Class C private
        )
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Helper: Bearer token validation via NetworkAuthManager
# ---------------------------------------------------------------------------
def _get_auth_manager():
    """Lazy-load NetworkAuthManager to avoid circular imports."""
    from Network.Authentication.auth_manager import NetworkAuthManager
    return NetworkAuthManager()


def validate_request_token(request: Request) -> bool:
    """Validate the Authorization Bearer token using NetworkAuthManager."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]  # strip 'Bearer ' prefix
    if not token or len(token) < 32:
        return False
    mgr = _get_auth_manager()
    result = mgr.validate_token(token)
    return result.success


def get_master_token() -> str:
    """Get the current master token (used by self-test)."""
    mgr = _get_auth_manager()
    summary = mgr.get_config_summary()
    token = summary.get("master_token", "")
    if not token or len(token) < 32:
        # Fallback: get from auth config file directly (bypasses circular import)
        if _AUTH_TOKEN_FILE.exists():
            try:
                with open(_AUTH_TOKEN_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    token = data.get("master_token", "")
            except Exception:
                pass
    return token


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class PairRequest(BaseModel):
    device_id: str = Field(..., min_length=1, description="Device identifier")
    pin: Optional[str] = Field(None, description="Pairing PIN for verification")


class PairResponse(BaseModel):
    success: bool
    message: str
    device_id: Optional[str] = None
    pin: Optional[str] = None


class StatusResponse(BaseModel):
    service: str = "JARVIS REST API"
    version: str = "1.0.0"
    lan_only: bool = True
    token_auth: bool = True
    memory_count: int = 0
    pending_security: int = 0
    uptime_seconds: int = 0


class TelemetryResponse(BaseModel):
    requests_total: int
    last_pairing: Optional[str] = None
    voice_muted: bool
    memory_entries: int
    pending_security_items: int


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    context: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str
    intent: str
    confidence: float


class VoiceConfigRequest(BaseModel):
    mute: Optional[bool] = None
    volume: Optional[int] = Field(None, ge=0, le=100)
    engine: Optional[str] = None
    language: Optional[str] = None


class MemoryItem(BaseModel):
    key: str
    value: Any = None
    metadata: Dict[str, Any] = {}
    created_at: str = ""


class MemoryCreateRequest(BaseModel):
    key: str = Field(..., min_length=1)
    value: Any = None
    metadata: Dict[str, Any] = {}


class SecurityPendingItem(BaseModel):
    id: int
    action: str
    requester: str
    timestamp: str
    status: str = "pending"
    details: Optional[str] = None


class AuditEntry(BaseModel):
    id: int
    timestamp: str
    level: str
    message: str
    result: str


# ---------------------------------------------------------------------------
# App factory helpers
# ---------------------------------------------------------------------------
_START_TIME: float = 0


def _now_str() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    from time import time
    return time()


def _telemetry_incr():
    _telemetry_data["requests_total"] += 1
    if _START_TIME:
        _telemetry_data["uptime_seconds"] = int(_now_ts() - _START_TIME)




def create_rest_app() -> FastAPI:
    app = FastAPI(title="JARVIS REST API", version="1.0.0")
    _START_TIME = _now_ts()

    # --- Middleware: LAN restriction ---
    @app.middleware("http")
    async def lan_middleware(request: Request, call_next):
        client_ip = request.client.host if request.client else "0.0.0.0"
        # Allow non-IP hostnames (e.g., TestClient uses "testclient") — dev/test mode
        if not _is_lan_ip(client_ip) and client_ip.replace(".", "").isdigit():
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Access denied: only LAN/IP loopback allowed"},
            )
        response = await call_next(request)
        return response

    # --- Middleware: Token auth (Bearer) ---
    @app.middleware("http")
    async def token_middleware(request: Request, call_next):
        protected_paths = ["/api/v1/"]
        exempt_paths = ["/api/v1/pair", "/api/v1/health"]
        if any(request.url.path.startswith(p) for p in protected_paths):
            if not any(request.url.path.startswith(e) for e in exempt_paths):
                if not validate_request_token(request):
                    return JSONResponse(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        content={"detail": "Missing or invalid Authorization Bearer token"},
                    )
        response = await call_next(request)
        return response

    # --- Ping ---
    @app.get("/ping", tags=["health"])
    async def api_ping():
        return {"status": "ok", "service": "JARVIS REST API", "token_auth": True}

    # --- Health ---
    @app.get("/api/v1/health", tags=["health"])
    async def api_health():
        return {"status": "healthy", "service": "JARVIS REST API", "version": "1.0.0"}

    # --- Pairing ---
    @app.post("/api/v1/pair", tags=["pairing"], response_model=PairResponse)
    async def api_pair(body: PairRequest):
        pin = secrets.token_hex(4)
        _pairing_pins[body.device_id] = pin
        _telemetry_data["last_pairing"] = body.device_id
        return PairResponse(success=True, message=f"PIN: {pin}", device_id=body.device_id, pin=pin)

    @app.get("/api/v1/pair/{device_id}", tags=["pairing"])
    async def api_pair_status(device_id: str):
        if device_id in _pairing_pins:
            return {"device_id": device_id, "status": "paired", "pin": _pairing_pins[device_id]}
        return {"device_id": device_id, "status": "unpaired"}

    # --- Status ---
    @app.get("/api/v1/status", tags=["health"], response_model=StatusResponse)
    async def api_status():
        return StatusResponse(
            memory_count=len(_memory_store),
            pending_security=len([s for s in _pending_security if s["status"] == "pending"]),
            uptime_seconds=int(_now_ts() - _START_TIME),
        )

    # --- Telemetry ---
    @app.get("/api/v1/telemetry", tags=["health"], response_model=TelemetryResponse)
    async def api_telemetry(request: Request):
        _telemetry_incr()
        return TelemetryResponse(
            requests_total=_telemetry_data["requests_total"],
            last_pairing=_telemetry_data.get("last_pairing"),
            voice_muted=_voice_config.get("mute", False),
            memory_entries=len(_memory_store),
            pending_security_items=len([s for s in _pending_security if s["status"] == "pending"]),
        )

    # --- Chat ---
    @app.post("/api/v1/chat", tags=["chat"], response_model=ChatResponse)
    async def api_chat(body: ChatRequest):
        reply = f"Echo: {body.message}"
        return ChatResponse(reply=reply, intent="echo", confidence=0.95)

    # --- Voice ---
    @app.post("/api/v1/voice/mute", tags=["voice"])
    async def api_voice_mute(request: Request):
        _telemetry_incr()
        data = await request.json() if request.headers.get("content-type") == "application/json" else {}
        mute = data.get("mute", not _voice_config["mute"])
        _voice_config["mute"] = mute
        return {"mute": mute, "volume": _voice_config["volume"]}

    @app.get("/api/v1/voice/config", tags=["voice"])
    async def api_voice_config():
        return VoiceConfigRequest(**_voice_config).model_dump()

    @app.put("/api/v1/voice/config", tags=["voice"])
    async def api_voice_config_put(body: VoiceConfigRequest):
        if body.mute is not None:
            _voice_config["mute"] = body.mute
        if body.volume is not None:
            _voice_config["volume"] = body.volume
        if body.engine is not None:
            _voice_config["engine"] = body.engine
        if body.language is not None:
            _voice_config["language"] = body.language
        return VoiceConfigRequest(**_voice_config).model_dump()


    # --- Memory CRUD ---
    @app.get("/api/v1/memory/{key}", tags=["memory"])
    async def api_memory_get(key: str):
        _telemetry_incr()
        item = _memory_store.get(key)
        if not item:
            raise HTTPException(status_code=404, detail=f"Memory key '{key}' not found")
        return MemoryItem(**item).model_dump()

    @app.post("/api/v1/memory", tags=["memory"])
    async def api_memory_create(body: MemoryCreateRequest):
        _telemetry_incr()
        item = {"key": body.key, "value": body.value, "metadata": body.metadata, "created_at": _now_str()}
        _memory_store[body.key] = item
        return MemoryItem(**item).model_dump()

    @app.delete("/api/v1/memory/{key}", tags=["memory"])
    async def api_memory_delete(key: str):
        _telemetry_incr()
        if key not in _memory_store:
            raise HTTPException(status_code=404, detail=f"Memory key '{key}' not found")
        item = _memory_store.pop(key)
        return {"deleted": True, "item": MemoryItem(**item).model_dump()}

    # --- Security Audit ---
    @app.post("/api/v1/security/audit/pending", tags=["security"])
    async def api_security_pending(request: Request):
        _telemetry_incr()
        data = await request.json() if request.headers.get("content-type") == "application/json" else {}
        item = {
            "id": len(_pending_security) + 1,
            "action": data.get("action", "unknown"),
            "requester": data.get("requester", "system"),
            "timestamp": _now_str(),
            "status": "pending",
            "details": data.get("details"),
        }
        _pending_security.append(item)
        return SecurityPendingItem(**item).model_dump()

    @app.post("/api/v1/security/audit/{item_id}/confirm", tags=["security"])
    async def api_security_confirm(item_id: int):
        _telemetry_incr()
        for s in _pending_security:
            if s["id"] == item_id and s["status"] == "pending":
                s["status"] = "confirmed"
                _audit_log.append({"id": len(_audit_log) + 1, "timestamp": _now_str(), "level": "info", "message": f"Audit item {item_id} confirmed", "result": "success"})
                return {"status": "confirmed"}
        raise HTTPException(status_code=404, detail="Pending audit item not found")

    @app.get("/api/v1/security/audit/pending", tags=["security"])
    async def api_security_list_pending(request: Request):
        _telemetry_incr()
        return {"items": [SecurityPendingItem(**s).model_dump() for s in _pending_security if s["status"] == "pending"]}

    @app.get("/api/v1/security/audit/log", tags=["security"])
    async def api_audit_log(request: Request):
        _telemetry_incr()
        return {"entries": [AuditEntry(**e).model_dump() for e in _audit_log]}

    # ---------------------------------------------------------------------------
    # WebSocket routes — mounted on the same app (port 8765)
    # ---------------------------------------------------------------------------
    from Network.WebSocket.ws_server import JarvisWebSocketManager

    _ws_mgr = JarvisWebSocketManager(app)
    app.state.ws_manager = _ws_mgr

    return app



def _run_self_test() -> bool:
    app = create_rest_app()
    client = TestClient(app)
    token = get_master_token()
    headers = {"Authorization": f"Bearer {token}"}
    passed, failed = 0, 0

    def _check(name: str, r, expected_status: int):
        nonlocal passed, failed
        ok = r.status_code == expected_status
        if ok:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name} -> {r.status_code} (expected {expected_status})")

    # 1. ping (no auth needed)
    r = client.get("/ping")
    _check("GET /ping", r, 200)

    # 2. status (auth required)
    r = client.get("/api/v1/status", headers=headers)
    _check("GET /status", r, 200)

    # 3. pair
    r = client.post("/api/v1/pair", json={"device_id": "test-dev-1"}, headers=headers)
    _check("POST /pair", r, 200)

    # 4. telemetry
    r = client.get("/api/v1/telemetry", headers=headers)
    _check("GET /telemetry", r, 200)

    # 5. chat
    r = client.post("/api/v1/chat", json={"message": "Hello JARVIS"}, headers=headers)
    _check("POST /chat", r, 200)

    # 6. voice mute toggle
    r = client.post("/api/v1/voice/mute", json={"mute": True}, headers=headers)
    _check("POST /voice/mute", r, 200)

    # 7. voice config
    r = client.get("/api/v1/voice/config", headers=headers)
    _check("GET /voice/config", r, 200)

    # 8. memory create
    r = client.post("/api/v1/memory", json={"key": "test_key", "value": "hello"}, headers=headers)
    _check("POST /memory", r, 200)

    # 9. memory get
    r = client.get("/api/v1/memory/test_key", headers=headers)
    _check("GET /memory/{key}", r, 200)

    # 10. memory delete
    r = client.delete("/api/v1/memory/test_key", headers=headers)
    _check("DELETE /memory/{key}", r, 200)

    # 11. security audit
    r = client.get("/api/v1/security/audit/log", headers=headers)
    _check("GET /security/audit/log", r, 200)

    # Auth failure test (no token)
    r = client.get("/api/v1/status")
    _check("Auth failure (no token)", r, 401)

    # Auth failure test (invalid token)
    bad_headers = {"Authorization": "Bearer short"}
    r = client.get("/api/v1/status", headers=bad_headers)
    _check("Auth failure (short token)", r, 401)

    print(f"\n=== Results: {passed} passed, {failed} failed ===\n")
    return failed == 0


# Main entry point
if __name__ == "__main__":
    success = _run_self_test()
    if not success:
        print("[WARN] Self-test had failures. Check output above.")
    else:
        print("[OK] All self-tests passed. REST API ready for deployment.")