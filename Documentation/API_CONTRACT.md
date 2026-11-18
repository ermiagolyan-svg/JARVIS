# C.E.L.E.S.T.E. REST & WebSocket API Contract — v2.0 Architecture

**System:** C.E.L.E.S.T.E. v2.0 (*Cognitive Engineering & Logic Execution System for Tactical Engagements*)  
**Version:** 2.0.0 (Unified REST & WebSocket Architecture)  
**Unified Port:** `8765` (All HTTP REST endpoints and duplex WebSocket channels run on the same port)  
**Base REST URL:** `http://<server-ip>:8765/api/v1/`  
**Base WebSocket URLs:** `ws://<server-ip>:8765/ws/lumia` and `ws://<server-ip>:8765/ws/desktop`  
**Auth Scheme:** Bearer Token (`Authorization: Bearer <token>` or query parameter `?token=<token>`)  
**Network Scope:** LAN-only (Strictly RFC 1918 private subnets and local loopback)

---

## 1. Unified Networking & Authentication

All communications pass through unified port `8765`. Both HTTP REST endpoints and WebSocket protocols share this port without requiring separate socket listeners or reverse proxies.

### 1.1 Authentication Scheme
- **REST Endpoints (`/api/v1/*`):** Require an `Authorization: Bearer <token>` header containing a 32+ hex character token managed by `NetworkAuthManager` (persisted in `Configuration/auth_token.json`).
- **WebSocket Endpoints (`/ws/*`):** Accept Bearer tokens either via the `Authorization: Bearer <token>` handshake header or via the query parameter `?token=<token>`.
- **Exempt Paths:** Liveness checks (`/ping`, `/health`, `/api/v1/health`) and initial device pairing (`/api/v1/pair`) are exempt from token authentication.

### 1.2 Security & Rate Limiting Middleware
- **RFC 1918 Private Range Enforcement:** Requests originating from non-LAN or non-loopback IP addresses receive an immediate **403 Forbidden**. Enforced ranges:
  - `127.0.0.0/8` (Loopback)
  - `10.0.0.0/8` (Class A private)
  - `172.16.0.0/12` (Class B private)
  - `192.168.0.0/16` (Class C private)
- **Brute-Force Lockout:** 5 failed authentication attempts within 60 seconds triggers a temporary IP lockout (60 seconds duration) managed by `NetworkAuthManager`.

---

## 2. WebSocket Endpoints (Port 8765)

### 2.1 `/ws/lumia` — Mobile & Remote Audio Streaming
- **Protocol:** Duplex WebSocket (Binary PCM + JSON control frames).
- **Audio Ingestion:** Binary 16-bit 16kHz Mono raw PCM audio chunks streamed directly from remote devices (e.g., Lumia smartphone).
- **Dynamic Mic Switching:**
  - When a client connects and streams audio to `/ws/lumia`, `JarvisWebSocketManager` sets `active_mic = "network"` and routes audio buffers to `VoiceOrchestrator.ingest_lumia_audio_chunk()`.
  - If the client disconnects or fails to send a heartbeat within 25.0 seconds, `active_mic` automatically fails over to `"mic"` (local physical soundcard) with zero pipeline interruption.
- **Control Frame Ingestion (JSON):**
  - Heartbeat: `{"type": "heartbeat"}`
  - Mute command: `{"type": "mute", "muted": true}`
  - Status query: `{"type": "status"}`

### 2.2 `/ws/desktop` — Desktop WPF HUD Telemetry & State Channel
- **Protocol:** Duplex WebSocket (JSON control & broadcast).
- **Authentication:** Validated via Bearer header or `?token=<token>` query string.
- **Broadcast Frame Schema (`hud_update`):** Broadcast periodically (up to 10Hz) to all connected Desktop UI clients.
```json
{
  "type": "hud_update",
  "state": "IDLE",
  "rms": 0.042,
  "lumia_connected": false,
  "model_loaded": true,
  "muted": false,
  "active_mic": "mic",
  "telemetry": {
    "cpu_percent": 14.2,
    "gpu_percent": 3.0,
    "gpu_temp_c": 44.0,
    "vram_used_mb": 4210.5,
    "vram_total_mb": 10240.0,
    "ram_used_gb": 12.8,
    "ram_total_gb": 32.0,
    "requests_total": 48,
    "last_pairing": "JarvisDesktopPC",
    "voice_muted": false
  },
  "pending_confirmations": [],
  "last_message": {
    "role": "assistant",
    "content": "درود، سامانه سلست نسخه ۲.۰ آماده اجرای فرامین است."
  }
}
```
- **State Enum Values:**
  - `IDLE`: Listening for wake word or standby.
  - `LISTENING`: Active user speech ingestion.
  - `THINKING`: Gemma-4-26B-A4B or Cloud LLM inference and tool reasoning.
  - `SPEAKING`: Dual-engine TTS synthesizing and playing audio.
  - `MUTED`: Audio pipeline disabled.
  - `AWAITING_CONFIRMATION`: High-risk command pending human approval.

---

## 3. REST API Endpoints (`/api/v1/*`)

### 3.1 Health & Telemetry

| Method | Path | Auth | Description |
|---|---|:---:|---|
| `GET` | `/ping` | ❌ | Liveness probe: `{"status": "ok", "service": "JARVIS REST API", "token_auth": true}` |
| `GET` | `/api/v1/health` | ❌ | Canonical health check used by `jarvis_watchdog.py`: `{"status": "ok", "healthy": true, "version": "1.0.0"}` |
| `GET` | `/health` | ❌ | Unauthenticated alias for `/api/v1/health` |
| `GET` | `/api/v1/status` | ✅ | Returns full system status including live LLM model name (`Gemma 4 26B-A4B IT (IQ4_XS)`), memory count, pending security items, and active mic |
| `GET` | `/api/v1/telemetry` | ✅ | Live hardware telemetry snapshot queried directly via `HardwareMonitor` (CPU %, GPU %, VRAM used/total MB, GPU temp C, RAM used/total GB, request counters) |

#### `/api/v1/status` Response Example
```json
{
  "service": "JARVIS REST API",
  "version": "1.0.0",
  "lan_only": true,
  "token_auth": true,
  "memory_count": 142,
  "pending_security": 0,
  "uptime_seconds": 3600,
  "state": "IDLE",
  "model_loaded": true,
  "model_name": "Gemma 4 26B-A4B IT (IQ4_XS)",
  "lumia_connected": false,
  "muted": false,
  "active_mic": "mic",
  "pairing_pin": "876500"
}
```

---

### 3.2 Pairing & Authentication

| Method | Path | Auth | Description |
|---|---|:---:|---|
| `POST` | `/api/v1/pair` | ❌ / ✅ | Pair a client device (e.g. desktop client or mobile). Body: `{"device_name": "JarvisDesktopPC", "pin": "876500"}`. Returns master auth token |
| `GET` | `/api/v1/pair/{device_id}` | ✅ | Check pairing status of a registered device ID |

---

### 3.3 Chat & Dialogue

| Method | Path | Auth | Description |
|---|---|:---:|---|
| `POST` | `/api/v1/chat` | ✅ | Send message to C.E.L.E.S.T.E. dialogue engine. Triggers AI inference and asynchronous TTS synthesis |

#### Request Payload (`ChatRequest`)
Supports both `text` (C# WPF client) and `message` fields:
```json
{
  "text": "وضعیت سیستم را گزارش کن",
  "message": "وضعیت سیستم را گزارش کن",
  "source": "desktop",
  "session_id": "default"
}
```

#### Response Payload (`ChatResponse`)
```json
{
  "spoken_response": "تمامی زیرسیستم‌های سلست در وضعیت پایدار قرار دارند.",
  "response": "تمامی زیرسیستم‌های سلست در وضعیت پایدار قرار دارند.",
  "reply": "تمامی زیرسیستم‌های سلست در وضعیت پایدار قرار دارند.",
  "intent": "ai_response",
  "confidence": 0.98,
  "language": "fa",
  "session_id": "default"
}
```

---

### 3.4 Voice Control & Dual-Engine TTS Configuration

| Method | Path | Auth | Description |
|---|---|:---:|---|
| `POST` | `/api/v1/voice/mute` | ✅ | Toggle or explicitly set audio mute state. Body: `{"muted": true}` or `{"mute": true}` |
| `GET` | `/api/v1/voice/config` | ✅ | Retrieve the active voice pipeline configuration including dual-engine settings and DSP filters |
| `POST` | `/api/v1/voice/config` | ✅ | Update voice configuration and DSP parameters live |
| `PUT` | `/api/v1/voice/config` | ✅ | Full update / replacement of voice configuration and DSP parameters live |

#### Dual-Engine Voice Config Schema (`VoiceConfigRequest`)
The voice pipeline supports dual engines:
1. **Gemini Online TTS (`engine_mode: "gemini"`):** High-fidelity cloud neural voice (`gemini-2.5-flash-preview-tts`), dedicated API key, selectable voices (`Kore`, `Puck`, `Fenrir`, `Aoede`), automatic pronunciation normalization.
2. **Gooya Bozorg v1.5 Local TTS (`engine_mode: "gooya"`):** 801M Chatterbox architecture on local CUDA, `bfloat16` precision, PyTorch `sdpa` FlashAttention, cached speaker conditioning, configurable `exaggeration` & `cfg_weight`, dynamic VRAM unloading.

```json
{
  "mute": false,
  "muted": false,
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
  "gooya_auto_unload": false,
  "dsp_pitch_semitones": -0.3,
  "pitch_semitones": -0.3,
  "dsp_metallic_mix": 0.0,
  "metallic_mix": 0.0,
  "dsp_reverb_mix": 0.0,
  "reverb_decay": 0.0,
  "wake_threshold": 0.50,
  "language": "fa-IR"
}
```

#### Behavior & Dynamic VRAM Unloading
- Incoming updates are persisted to `Configuration/config.json` via `ConfigManager`.
- Live DSP parameters are applied immediately to `CelesteAudioDSP` (pitch shift, comb filter, convolution reverb).
- Live wake threshold is applied directly to `WakeWordEngine`.
- Live engine switching triggers `TTSEngine.update_config()`:
  - Switching to `"gooya"` initializes or wakes the local Gooya worker on the RTX 3080.
  - Switching to `"gemini"` or setting `gooya_auto_unload: true` triggers `unload_gooya()`, releasing 100% of Gooya's VRAM back to the RTX 3080 for MoE inference.

---

### 3.5 Persistent Memory & SQLite Facts (`user_facts`)

The memory subsystem is fully backed by SQLite (`Data/Memory/jarvis_memory.db`) via `MemoryManager`.

| Method | Path | Auth | Description |
|---|---|:---:|---|
| `GET` | `/api/v1/memory/facts` | ✅ | List all persistent facts from SQLite `user_facts`. Optional query param `?category=user` |
| `POST` | `/api/v1/memory/facts` | ✅ | Create or upsert a persistent fact into SQLite `user_facts` |
| `DELETE` | `/api/v1/memory/facts/{fact_id_or_key}` | ✅ | Delete a persistent fact from SQLite by numeric `id` or string `fact_key` |
| `GET` | `/api/v1/memory/history` | ✅ | Retrieve recent conversation history records (up to 40) from SQLite |
| `DELETE` | `/api/v1/memory/history` | ✅ | Clear conversation history in SQLite and DialogueManager memory |
| `GET` | `/api/v1/memory/{key}` | ✅ | Get legacy key-value memory item (returns 404 if not found) |
| `POST` | `/api/v1/memory` | ✅ | Set legacy key-value memory item |
| `DELETE` | `/api/v1/memory/{key}` | ✅ | Delete legacy key-value memory item |

#### `POST /api/v1/memory/facts` Payload (`MemoryCreateRequest`)
Supports field aliases matching both C# WPF client (`fact_key`, `fact_value`) and REST tests (`key`, `value`):
```json
{
  "category": "user",
  "key": "user_preference_editor",
  "value": "Visual Studio Code",
  "importance": 8,
  "metadata": {
    "source": "voice_dialogue"
  }
}
```

#### `GET /api/v1/memory/facts` Response Example
```json
{
  "facts": [
    {
      "id": 1,
      "category": "user",
      "key": "user_preference_editor",
      "fact_key": "user_preference_editor",
      "value": "Visual Studio Code",
      "fact_value": "Visual Studio Code",
      "importance": 8,
      "updated_at": "2026-11-18 22:30:00"
    }
  ]
}
```

#### `DELETE /api/v1/memory/facts/{fact_id_or_key}` Response
Supports passing either an integer SQLite primary key `id` (`DELETE /api/v1/memory/facts/1`) or string key (`DELETE /api/v1/memory/facts/user_preference_editor`):
```json
{
  "deleted": true,
  "key": "user_preference_editor",
  "id": "user_preference_editor"
}
```

---

### 3.6 Security Audit & Human Confirmation

C.E.L.E.S.T.E. enforces a 4-Tier Security Gatekeeper (`SAFE`, `MODERATE`, `SENSITIVE`, `DANGEROUS`). Sensitive and dangerous commands require explicit human confirmation.

| Method | Path | Auth | Description |
|---|---|:---:|---|
| `GET` | `/api/v1/security/pending` | ✅ | List all pending security authorization requests (alias for `/security/audit/pending`) |
| `GET` | `/api/v1/security/audit/pending` | ✅ | List all pending security authorization requests |
| `POST` | `/api/v1/security/audit/pending` | ✅ | Submit a new pending authorization item to the queue |
| `POST` | `/api/v1/security/confirm` | ✅ | Confirm or reject a pending security action via JSON body (supports `action_id` and `item_id`) |
| `POST` | `/api/v1/security/audit/{item_id}/confirm` | ✅ | Confirm a pending security action by path parameter |
| `GET` | `/api/v1/security/audit` | ✅ | Retrieve the security audit log entries (alias for `/security/audit/log`) |
| `GET` | `/api/v1/security/audit/log` | ✅ | Retrieve the full security audit log entries from SQLite & memory |

#### `POST /api/v1/security/confirm` (Body-Based Confirmation)
Used by Desktop UI and automation scripts:
```json
{
  "action_id": "1",
  "item_id": 1,
  "approved": true
}
```
**Response:**
```json
{
  "status": "confirmed",
  "approved": true
}
```
If `approved: false` is supplied, the item status becomes `"rejected"`.

#### `POST /api/v1/security/audit/{item_id}/confirm` (Path-Based Confirmation)
Used by quick-action HTTP webhooks and notifications:
- URL: `/api/v1/security/audit/1/confirm`
- Body: None required (empty POST)
- **Response:** `{"status": "confirmed"}`

---

### 3.7 Structured Logs

| Method | Path | Auth | Description |
|---|---|:---:|---|
| `GET` | `/api/v1/logs` | ✅ | Returns up to 120 recent structured log entries parsed from active logger memory buffer and log files |

#### Log Entry Format (`LogEntryItem`)
```json
{
  "logs": [
    {
      "timestamp": "23:15:02",
      "level": "INFO",
      "module": "rest_server",
      "message": "Voice config updated: engine_mode=gemini"
    }
  ]
}
```

---

## 4. Error Responses

| Status Code | Meaning | Response Body |
|---|---|---|
| `401 Unauthorized` | Missing or invalid Bearer token | `{"detail": "Missing or invalid Authorization Bearer token"}` |
| `403 Forbidden` | Non-LAN or non-loopback IP address | `{"detail": "Access denied: only LAN/IP loopback allowed"}` |
| `404 Not Found` | Requested memory fact, key, or security item not found | `{"detail": "Fact '...' not found"}` |
| `422 Unprocessable Entity` | Pydantic validation error or missing required fields | `{"detail": "Missing required field 'key' or 'fact_key'"}` |
| `500 Internal Server Error` | Database or unhandled service exception | `{"detail": "Database error: ..."}` |

---

## 5. Verification & Testing

Every update to REST and WebSocket endpoints is verified with non-blocking self-tests conforming to `.clinerules`:
```bash
python -m Network.API.rest_server
```
All 18 integrated assertions validate:
- Unified port `8765` route bindings.
- Liveness `/ping`, `/api/v1/status` reporting `Gemma 4 26B-A4B IT (IQ4_XS)`.
- Live hardware telemetry snapshots.
- Dual-engine voice config updates (`gemini` and `gooya`).
- SQLite-backed memory facts creation, retrieval, and deletion.
- Security confirmation queues.
- LAN and Bearer token security enforcement.
