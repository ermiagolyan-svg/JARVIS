# J.A.R.V.I.S. Local API & WebSocket Contract (Port 8765)

## 1. Authentication
- Public Endpoints: POST /api/v1/pair, GET /api/v1/health
- Protected Endpoints: All other /api/v1/* routes require header Authorization: Bearer <token>
- POST /api/v1/pair: Request {"device_name": "Lumia950XL", "pin": "123456"} -> Response {"status": "paired", "token": "<256bit_hex>", "expires_in": 0}

## 2. REST Endpoints (http://<PC_IP>:8765)
- GET /api/v1/status: Returns {"state": "IDLE", "model_loaded": true, "model_name": "Qwen 3.5 9B", "lumia_connected": false, "muted": false, "active_mic": "mic", "pairing_pin": "123456"}
- GET /api/v1/telemetry: Returns {"cpu_percent": 12.5, "gpu_percent": 24.0, "vram_used_mb": 6100, "vram_total_mb": 10240, "gpu_temp_c": 52, "ram_used_gb": 11.2, "ram_total_gb": 32.0}
- POST /api/v1/chat: Request {"text": "سلام جارویس", "source": "desktop"} -> Response {"spoken_response": "...", "action": null, "language": "fa", "session_id": "default"}
- POST /api/v1/voice/mute: Request {"muted": true} -> Response {"muted": true}
- POST /api/v1/voice/config: Request {"pitch_semitones": -2.5, "metallic_mix": 0.22, "reverb_decay": 0.18, "wake_threshold": 0.5} -> Response {"status": "updated"}
- GET /api/v1/memory/history: Returns {"messages": [{"id": 1, "role": "user", "content": "...", "timestamp": "..."}]}
- DELETE /api/v1/memory/history: Returns {"status": "cleared"}
- GET /api/v1/memory/facts: Returns {"facts": [{"id": 1, "category": "user", "key": "name", "value": "Ilia", "importance": 5}]}
- POST /api/v1/memory/facts: Request {"category": "user", "key": "...", "value": "...", "importance": 5} -> Response {"status": "saved"}
- DELETE /api/v1/memory/facts/{fact_id}: Returns {"status": "deleted"}
- GET /api/v1/security/pending: Returns {"pending": [{"action_id": "...", "command_type": "...", "risk_level": "SENSITIVE", "payload": "..."}]}
- POST /api/v1/security/confirm: Request {"action_id": "...", "approved": true} -> Response {"status": "executed"}
- GET /api/v1/security/audit: Returns {"audit_logs": [{"id": 1, "command_type": "...", "risk_level": "SAFE", "status": "GRANTED", "timestamp": "..."}]}
- GET /api/v1/logs: Returns {"logs": [{"timestamp": "...", "level": "INFO", "module": "...", "message": "..."}]}

## 3. WebSocket Endpoints (ws://<PC_IP>:8765)
- URLs: /ws/lumia?token=<token> and /ws/desktop?token=<token>
- Binary Frame (/ws/lumia -> Server): Raw PCM 16000Hz, 16-bit signed int, mono (2560 bytes per 80ms chunk).
- Incoming JSON Frame: {"type": "heartbeat" | "mute_toggle" | "wake_trigger" | "confirm_action" | "text_command", "payload": {}}
- Outgoing JSON Frame (hud_update): {"type": "hud_update", "state": "IDLE", "rms": 0.02, "lumia_connected": true, "model_loaded": true, "muted": false, "active_mic": "network", "telemetry": {}, "pending_confirmations": [], "last_message": {}}
"@ | Set-Content -Path "D:\AI\JARVIS\Documentation\API_CONTRACT.md" -Encoding UTF8

@"
# JARVIS Project Status
- Stage 1.1 to 1.3 (Config, Logger, SQLite, MemoryManager): COMPLETED & VERIFIED
- Stage 2.1 to 2.2 (LlamaCppManager, LLMClient, DialogueManager): COMPLETED & VERIFIED
- Stage 3.1 to 3.4 (STT, TTS, DSP, WakeWord, VoiceOrchestrator): COMPLETED & VERIFIED
- Stage 4.1 to 4.3 (4-Tier Security, HardwareMonitor, AutomationRouter): COMPLETED & VERIFIED
- Stage 5.1 to 5.2 (NetworkAuthManager, FastAPI REST Server): COMPLETED & VERIFIED
- Stage 5.3 (IP-based brute-force rate limiting in `pair_device`, `_is_lan_ip` refactor to `ipaddress` module): COMPLETED & VERIFIED
