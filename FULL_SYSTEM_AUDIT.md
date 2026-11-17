# FULL SYSTEM AUDIT & TECHNICAL BLUEPRINT
**Target System:** JARVIS / C.E.L.E.S.T.E. (D:\AI\JARVIS)  
**Audit Date:** 2026-11-14  
**Auditor Role:** Read-Only Principal Software Architect & Deep Codebase Auditor  

---

## 1. Exact Class & Method Signatures Inventory

### LAYER 1 — Config, Logger & SQLite Memory

#### `Core/Config/config_loader.py`
- **Functions:**
  - `get_config() -> ConfigManager`
  - `load_config() -> bool`
  - `save_config() -> bool`
  - `_deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]`
- **Class `ConfigManager` (Singleton):**
  - `get_instance() -> ConfigManager`
  - `load() -> bool`
  - `save() -> bool`
  - `get(section: str, key: str, default: Any = None) -> Any`
  - `set(section: str, key: str, value: Any) -> bool`
  - `get_section(section: str) -> Dict[str, Any]`
  - `validate() -> list`
  - `restore_defaults() -> bool`
  - `reload() -> bool`
  - `to_dict() -> Dict[str, Any]`

#### `Core/Logger/logger.py`
- **Functions:**
  - `get_logger(module_name: str) -> JarvisLogger`
  - `get_recent_logs() -> List[str]`
  - `get_recent_logs_count() -> int`
  - `clear_recent_logs() -> None`
- **Class `JarvisLogger`:**
  - `__init__(name: str) -> None`
  - `debug(msg: str) -> None`
  - `info(msg: str) -> None`
  - `warning(msg: str) -> None`
  - `error(msg: str) -> None`
  - `critical(msg: str) -> None`
  - `get_logger() -> logging.Logger`

#### `Core/Memory/db_manager.py`
- **Functions:**
  - `get_db() -> DatabaseManager`
  - `init_db() -> DatabaseManager`
  - `get_db_path() -> Path`
- **Class `DatabaseManager`:**
  - `__init__() -> None`
  - `execute_query(query: str, params: Tuple = ()) -> None`
  - `fetch_one(query: str, params: Tuple = ()) -> Optional[sqlite3.Row]`
  - `fetch_all(query: str, params: Tuple = ()) -> List[sqlite3.Row]`
  - `close() -> None`

#### `Core/Memory/memory_manager.py`
- **Class `MemoryManager`:**
  - `__init__() -> None`
  - `load_settings() -> None`  *(Note: Returns None, NOT bool)*
  - `add_message(session_id: str, role: str, content: str, language: str = 'fa') -> None`
  - `get_recent_context(limit: int = 20) -> List[sqlite3.Row]`
  - `search_conversations(query: str) -> List[sqlite3.Row]`
  - `set_fact(category: str, key: str, value: str, importance: int = 5) -> None`
  - `get_facts_prompt_block() -> str`
  - `delete_fact(fact_id: Optional[int] = None, fact_key: Optional[str] = None) -> bool`
  - `clear_history(session_id: Optional[str] = None) -> int`
  - `cleanup_old_memories(days: int = 365) -> int`
  - `set_preference(key: str, value: Any) -> None`
  - `get_preference(key: str, default: Optional[str] = None) -> Any`

---

### LAYER 2 — AI Engine, LLM Router & Dialogue Manager

#### `Core/ModelManager/llamacpp_manager.py`
- **Alias:** `ModelManager = LlamaCppManager`
- **Class `LlamaCppManager`:**
  - `__init__() -> None`
  - `@property host -> str`
  - `@property port -> int`
  - `@property base_url -> str`
  - `is_server_running() -> bool`
  - `check_health() -> bool`
  - `start_server(model_path: Optional[str] = None) -> bool`
  - `stop_server() -> bool`
  - `stop_managed_server() -> bool`

#### `Core/AI/llm_client.py`
- **Class `LLMClient`:**
  - `__init__(base_url=None, model_name=None) -> None`
  - `detect_requested_provider(messages: List[Dict[str, str]], override_provider: Optional[str] = None) -> str`
  - `chat(messages: List[Dict[str, str]], provider: Optional[str] = None) -> Dict[str, Any]`
  - `chat_stream(messages: List[Dict[str, str]], provider: Optional[str] = None) -> Generator[str, None, None]`
  - `get_health_status() -> bool`
  - `set_model(model_name: str) -> None`

#### `Core/Conversation/dialogue_manager.py`
- **Functions:**
  - `parse_actions(text: str) -> tuple`
- **Class `DialogueManager`:**
  - `__init__(llm_client=None) -> None`
  - `process_user_message(user_message: str, session_id: str = "default") -> str`
  - `process_user_message_stream(user_message: str, session_id: str = "default") -> Generator[str, None, None]`
  - `clear_history() -> None`
### LAYER 3 — Voice Pipeline (STT, TTS, DSP, WakeWord, Orchestrator)

#### `Voice/STT/stt_engine.py`
- **Class `STTEngine`:**
  - `__init__(model_size=None, device=None, compute_type=None, cpu_threads=None) -> None`
  - `initialize() -> bool`
  - `transcribe_pcm(pcm_bytes, sample_rate=16000, language=None) -> Dict[str, Any]`
  - `shutdown() -> None`  *(Note: Method name is `shutdown()`, NOT `close()`)*

#### `Voice/AudioDSP/jarvis_dsp.py`
- **Alias:** `CelesteAudioDSP = JarvisAudioDSP`
- **Class `JarvisAudioDSP`:**
  - `__init__(pitch_semitones: Optional[float] = None, sample_rate: int = 24000, speech_rate: Optional[float] = None, metallic_mix: Optional[float] = None, reverb_decay: Optional[float] = None) -> None`
  - `@classmethod set_global_config(cls, pitch_semitones=None, metallic_mix=None, reverb_decay=None, speech_rate=None)`
  - `update_config(pitch_semitones=None, metallic_mix=None, reverb_decay=None, speech_rate=None)`
  - `process(pcm: np.ndarray) -> np.ndarray`

#### `Voice/TTS/tts_engine.py`
- **Class `TTSEngine`:**
  - `__init__(model_name: Optional[str] = None) -> None`
  - `initialize() -> bool`
  - `get_status() -> Dict[str, Any]`
  - `speak(text: str, blocking: bool = False, speech_generation_id: Optional[int] = None) -> None`
  - `synthesize_raw(text: str) -> Tuple[Optional[np.ndarray], int]`
  - `stop_speaking() -> None`

#### `Voice/WakeWord/wakeword_engine.py`
- **Class `WakeWordEngine`:**
  - `__init__(on_wake_word_detected: Optional[Callable] = None, speech_recorded_callback: Optional[Callable] = None) -> None`
  - `initialize() -> bool`
  - `start() -> None`
  - `stop() -> None`
  - `set_muted(muted: bool) -> None`
  - `is_muted() -> bool`
  - `get_current_rms() -> float`
  - `set_audio_source(source: str) -> None`
  - `feed_audio(pcm_bytes: bytes) -> None`

#### `Voice/voice_orchestrator.py`
- **Enum `VoiceState`:** `IDLE`, `LISTENING`, `THINKING`, `SPEAKING`, `MUTED`, `AWAITING_CONFIRMATION`
- **Class `VoiceOrchestrator`:**
  - `__init__(on_state_change=None) -> None`
  - `initialize() -> bool`
  - `start() -> None`
  - `stop() -> None`
  - `get_state() -> VoiceState`
  - `set_state(state: VoiceState) -> None`
  - `is_muted() -> bool`
  - `mute() -> None`
  - `unmute() -> None`
  - `barge_in() -> None`
  - `get_status() -> dict`

---

### LAYER 4 — Automation, Security, Web Search & Hardware Telemetry

#### `Desktop/Monitoring/hardware_monitor.py`
- **DataClasses:** `CpuMetrics`, `GpuMetrics`, `RamMetrics`, `DiskMetrics`, `TelemetrySnapshot`
- **Class `HardwareMonitor`:**
  - `__init__() -> None`
  - `get_telemetry_snapshot() -> TelemetrySnapshot`
  - `get_persian_status_summary() -> str`

#### `Automation/Security/permission_manager.py`
- **Functions:**
  - `get_permission_manager() -> SecurityPermissionManager`
  - `run_self_test() -> None`
- **Enum `PermissionLevel(IntEnum)`:** `SAFE=1`, `MODERATE=2`, `SENSITIVE=3`, `DANGEROUS=4`
- **Class `PermissionResult`:** `__init__(granted: bool, level: PermissionLevel, reason: str)`
- **Class `SecurityPermissionManager`:**
  - `check_permission(command: str) -> PermissionResult`
  - `confirm_pending_action(action_id: str, approved: bool) -> bool`
  - `get_audit_entries(limit: int = 50) -> List[sqlite3.Row]`
  - `get_alert_log() -> List[Dict[str, Any]]`
  - `get_pending_confirmations() -> Dict[str, Tuple[datetime, str]]`

#### `Automation/Windows/system_control.py`
- **Functions:**
  - `get_system_control() -> SystemControl`
- **Enum `SystemControlRisk(IntEnum)`:** `SAFE=1`, `MODERATE=2`, `SENSITIVE=3`, `DANGEROUS=4`
- **Class `SystemControl`:**
  - `set_volume(level: int) -> Dict[str, Any]`
  - `get_volume() -> Dict[str, Any]`
#### `Automation/Applications/app_manager.py`
- **Functions:**
  - `get_app_manager() -> AppManager`
- **Enum `AppAction(IntEnum)`:** `LAUNCH=1`, `CLOSE=2`, `KILL=3`, `STATUS=4`, `LIST=5`
- **Class `AppManager`:**
  - `launch(app_name: str, args: Optional[List[str]] = None) -> Dict[str, Any]`
  - `launch_app(name: str) -> Dict[str, Any]`
  - `close_app(name: str) -> Dict[str, Any]`
  - `close(app_name: str) -> Dict[str, Any]`
  - `kill(app_name: str) -> Dict[str, Any]`
  - `get_status(name: Optional[str] = None) -> Dict[str, Any]`
  - `get_known_apps() -> Dict[str, Any]`
  - `status(app_name: str) -> Dict[str, Any]`
  - `list_apps() -> List[str]`
  - `get_launch_log() -> List[Dict[str, Any]]`

#### `Automation/FileSystem/file_manager.py`
- **Functions:**
  - `get_file_manager() -> FileManager`
- **Enum `FileAction(IntEnum)`:** `SEARCH=1`, `READ=2`, `WRITE=3`, `DELETE=4`, `LIST=5`, `SCAN=6`
- **Enum `SensitiveFileRisk(IntEnum)`:** `NONE=0`, `LOW=1`, `MEDIUM=2`, `HIGH=3`
- **Class `FileManager`:**
  - `search(pattern: str, root_dir: Optional[str] = None, max_results: int = 50) -> Dict[str, Any]`
  - `read_file(filepath: str) -> Dict[str, Any]`
  - `write_file(filepath: str, content: str) -> Dict[str, Any]`
  - `delete_file(filepath: str) -> Dict[str, Any]`
  - `list_dir(dirpath: str) -> Dict[str, Any]`
  - `scan_sensitive(filepath: str) -> Dict[str, Any]`
  - `get_operation_log() -> List[Dict[str, Any]]`

#### `Automation/Web/web_search.py`
- **Functions:**
  - `get_web_search_engine() -> WebSearchEngine`
- **Class `WebSearchEngine`:**
  - `search(query: str, limit: int = 4) -> Dict[str, Any]`
  - `format_results_for_prompt(search_data: Dict[str, Any]) -> str`

#### `Automation/automation_router.py`
- **Functions:**
  - `get_automation_router(role: Role = Role.USER) -> AutomationRouter`
- **Enum `Role(IntEnum)`:** `GUEST=1`, `USER=2`, `ADMIN=3`
- **Class `AutomationRouter`:**
  - `check_permission(command: str) -> PermissionResult`
  - `execute(command: str) -> Dict[str, Any]`

---

### LAYER 5 — Network & Master Orchestrator

#### `Network/Authentication/auth_manager.py`
- **Functions:**
  - `get_auth_manager() -> NetworkAuthManager`
  - `run_self_test() -> None`
  - `_is_lan_ip(ip_address: str) -> bool`
- **Classes:** `AuthResult`, `PairingResult`
- **Class `NetworkAuthManager` (Singleton):**
  - `get_instance() -> NetworkAuthManager`
  - `validate_source_ip(ip_address: str) -> AuthResult`
  - `validate_token(token: str) -> AuthResult`
  - `pair_device(device_name: str) -> PairingResult`
  - `verify_pairing_pin(device_id: str, pin: str) -> bool`
  - `revoke_token() -> bool`
  - `revoke_device(device_id: str) -> bool`
  - `get_paired_devices() -> List[Dict[str, Any]]`
  - `get_revocation_count() -> int`
  - `get_config_summary() -> Dict[str, Any]`

#### `Network/API/rest_server.py`
- **Functions:**
  - `create_rest_app() -> FastAPI`
  - `get_master_token() -> str`
  - `validate_request_token(request: Request) -> bool`
  - `_run_self_test() -> bool`

#### `Network/WebSocket/ws_server.py`
- **Class `JarvisWebSocketManager`:**
  - `__init__(app: FastAPI, voice_orchestrator=None, auth_manager=None)`
  - `handle_lumia_connection(websocket: WebSocket) -> None`
  - `handle_desktop_connection(websocket: WebSocket) -> None`
  - `broadcast_to_desktop(data: Dict[str, Any]) -> None`
  - `broadcast_hud_update(data: Dict[str, Any]) -> None`
  - `get_state() -> Dict[str, Any]`

#### `Core/jarvis_main.py`
- **Functions:**
  - `_acquire_single_instance_mutex() -> bool`
- **Class `JarvisMasterService`:**
  - `__init__(dry_run: bool = False)`
  - `initialize() -> bool`
  - `run() -> None`

#### `Core/jarvis_watchdog.py`
- **Functions:**
  - `run_watchdog(test_once: bool = False) -> None`

#### `Automation/Windows/startup_manager.py`
- **Functions:**
  - `register_startup() -> bool`
  - `is_registered() -> bool`
  - `unregister_startup() -> bool`
  - `get_startup_info() -> dict`

---

### LAYER 6 — Desktop WPF Application (.NET 8 C#)

#### `Desktop/UI/Services/JarvisBackendClient.cs`
- **Class `JarvisBackendClient: IDisposable`:**
  - **Properties:** `BaseHttpUrl`, `BaseWsUrl`, `IsWsConnected`, `PairingPin`
  - **Events:** `OnHudUpdate`, `OnConnectionStateChanged`, `OnClientLog`
  - **Methods:**
    - `LoadLocalTokenAndPin()`
    - `ConnectWebSocketAsync()`
    - `GetSystemStatusAsync()`
    - `GetTelemetryAsync()`
    - `SendChatMessageAsync(string text)`
    - `GetMemoryHistoryAsync()`
    - `GetMemoryFactsAsync()`
    - `SaveFactAsync(string category, string key, string value, int importance = 5)`
    - `DeleteFactAsync(int factId)`
    - `GetPendingConfirmationsAsync()`
    - `ConfirmSecurityActionAsync(string actionId, bool approved)`
    - `GetSecurityAuditAsync()`
    - `GetLogsAsync()`
    - `IsWindowsStartupEnabled()`
    - `SetWindowsStartupEnabled(bool enabled)`
    - `Dispose()`

#### `Desktop/UI/Models/JarvisModels.cs`
- **Classes:**
  - `PairRequest`, `PairResponse`
  - `SystemStatusModel`
  - `TelemetryModel`
  - `ChatRequest`, `ChatResponse`
  - `ConversationItem`, `MemoryHistoryResponse`
  - `FactItem`, `MemoryFactsResponse`, `FactCreateRequest`
  - `PendingConfirmationItem`, `PendingConfirmationsResponse`, `ConfirmActionRequest`
  - `AuditLogItem`, `AuditLogResponse`
  - `LogEntryItem`, `LogsResponse`
  - `VoiceConfigModel`, `MuteRequest`, `MuteResponse`
  - `HudUpdateMessage`

---

## 2. Cross-Module Signature & Import Mismatches

| # | File Calling (File A) | Target File (File B) | Call Site / Code Snippet | Mismatch Details |
|---|----------------------|----------------------|--------------------------|------------------|
| 1 | `Core/jarvis_main.py` (Line 84) | `Core/Memory/memory_manager.py` | `if not self.memory.load_settings():` | `load_settings()` in `memory_manager.py` returns `None` (implicit), NOT `bool`. Evaluating `if not None:` evaluates `True` on every run, logging a false warning: `"Memory settings could not be loaded (non-fatal)"`. |
| 2 | `Core/jarvis_main.py` (Line 285) | `Voice/STT/stt_engine.py` | `if self.stt_engine and hasattr(self.stt_engine, 'close'): self.stt_engine.close()` | `STTEngine` does NOT have a `close()` method (its cleanup method is named `shutdown()`). The `hasattr` check fails silently, leaving Whisper engine unclosed on shutdown. |
| 3 | `Network/API/rest_server.py` (Line 434) | `Desktop/Monitoring/hardware_monitor.py` | `GET /api/v1/telemetry` | `rest_server.py` does not call `HardwareMonitor().get_telemetry_snapshot()`. It returns static mock telemetry (12.4% GPU, 2450MB VRAM, 48.5C GPU temp, 14.2GB RAM). |
| 4 | `Network/API/rest_server.py` (Line 712) | `Voice/TTS/tts_engine.py` & `Voice/voice_orchestrator.py` | `POST /api/v1/voice/config` | `rest_server.py` updates DSP parameters via `JarvisAudioDSP.set_global_config`, but does NOT notify or update `TTSEngine` or `VoiceOrchestrator` when state changes occur. |
| 5 | `Network/API/rest_server.py` (Line 777) | `Core/Memory/memory_manager.py` | `DELETE /api/v1/memory/facts/{fact_key_or_id}` | `JarvisBackendClient.cs` passes integer IDs (`DELETE /api/v1/memory/facts/12`). Fallback dictionary store in `rest_server.py` (`_facts_fallback_store`) uses `fact_key` as strings, causing an unhandled KeyError when SQLite `mm` is not initialized. |

---

## 3. REST & WebSocket vs. C# WPF Contract Mismatches

| Component | Path / Route | Method / Frame | C# Model (`JarvisModels.cs`) | Python Implementation (`rest_server.py` / `ws_server.py`) | Status / Mismatch Details |
|-----------|--------------|----------------|------------------------------|---------------------------------------------------------|---------------------------|
| REST | `/api/v1/telemetry` | `GET` | `TelemetryModel`: `cpu_percent`, `gpu_percent`, `vram_used_mb`, `vram_total_mb`, `gpu_temp_c`, `ram_used_gb`, `ram_total_gb` | Returns matching JSON keys, BUT values are hardcoded mock floats instead of real hardware telemetry from `HardwareMonitor`. | ⚠️ Data Mismatch (Mock values returned) |
| REST | `/api/v1/security/confirm` | `POST` | `ConfirmActionRequest`: `action_id`, `approved` | `rest_server.py` defines `POST /api/v1/security/confirm` AND `POST /api/v1/security/audit/{item_id}/confirm`. C# uses `/security/confirm`. | ✅ Compatible |
| REST | `/api/v1/memory/facts/{id}` | `DELETE` | `DeleteFactAsync(int factId)` | Expects integer ID or key string. Handled correctly when SQLite `mm` is online. | ✅ Compatible |
| WS | `/ws/desktop` | `WebSocket` | Listens for `HudUpdateMessage` (`type: "hud_update"`). Contains `state`, `rms`, `lumia_connected`, `model_loaded`, `muted`, `active_mic`, `telemetry`, `pending_confirmations`, `last_message`. | Sends `hud_update` frames with all expected keys. | ✅ Fully Matched |
| WS | `/ws/lumia` | `WebSocket` | Lumia client frame ingest (binary 16kHz PCM audio stream + JSON metadata). | Ingests binary PCM chunks and updates active mic state to `"network"`. Failovers back to `"mic"` on disconnect. | ✅ Fully Matched |

---

## 4. Voice, TTS, DSP & AI Pipeline Bugs

1. **`Voice/TTS/tts_engine.py` — Blocking Audio Playback:**
   - **Bug:** Line 381 calls `winsound.PlaySound(buf.getvalue(), winsound.SND_MEMORY)` synchronously.
   - **Impact:** Calling `speak(..., blocking=True)` or calling `speak()` from an HTTP request / VoiceOrchestrator thread completely freezes the process during playback.
   - **Fix:** Add `winsound.SND_ASYNC` when non-blocking playback is requested, or wrap sound playback in a background daemon thread.

2. **`Voice/TTS/tts_engine.py` — Edge-TTS Fallback Event Loop Crash:**
   - **Bug:** Line 341 calls `asyncio.run(_fetch())` inside `_synthesize_edge_fallback`.
   - **Impact:** If `speak()` is triggered from an event loop thread (e.g. FastAPI / Uvicorn worker thread), `asyncio.run()` raises `RuntimeError: asyncio.run() cannot be called from a running event loop`.
   - **Fix:** Use `asyncio.get_event_loop().run_until_complete()` or run in a thread pool executor.

3. **`Voice/AudioDSP/jarvis_dsp.py` — Sample Rate Mismatch & Re-instantiation:**
   - **Bug:** `TTSEngine` instantiates `JarvisAudioDSP(sample_rate=sample_rate)` on every single synthesis request. `WakeWordEngine` and `STTEngine` operate at `16000` Hz, while `TTSEngine` and `JarvisAudioDSP` default to `24000` Hz.
   - **Impact:** Activation chime in `VoiceOrchestrator` generated at `22050` Hz is passed to `JarvisAudioDSP` operating at `24000` Hz, causing frequency distortion.
   - **Fix:** Pass explicit `sample_rate` to `JarvisAudioDSP` matching the audio source.

4. **`Core/Conversation/dialogue_manager.py` — Direct Command Execution Security Bypass:**
   - **Bug:** Lines 302, 310, 320 execute actions directly:
     `subprocess.run(cmd, shell=True, ...)`
     `open(path, 'r', ...)`
   - **Impact:** Actions generated by LLM (`<action>{"type": "run_command", ...}</action>`) bypass `SecurityPermissionManager` and `AutomationRouter` entirely!
   - **Fix:** Route all actions from `DialogueManager` through `AutomationRouter.execute()`.

5. **`Core/AI/llm_client.py` — Empty Conversation Sanitization:**
   - **Bug:** `_sanitize_messages` strips system messages into `sys_parts`. If `messages` contains only system prompt, `conv` is empty and `_sanitize_messages` returns `[]`.
   - **Impact:** Calling `chat()` with only a system message causes an empty payload error.
   - **Fix:** Ensure `_sanitize_messages` returns at least one user message if `conv` is empty.

---

## 5. Prioritized Step-by-Step Fix Plan (Grouped into small tasks for Qwen 35B)

### TASK 1: Fix Core Service & Memory Manager Return Mismatches
- **Target Files:**
  - `Core/Memory/memory_manager.py` (Line 24)
  - `Voice/STT/stt_engine.py` (Line 117)
  - `Core/jarvis_main.py` (Lines 84, 285)
- **Changes Required:**
  1. In `memory_manager.py`, update `load_settings()` to `return True` after loading settings.
  2. In `stt_engine.py`, add alias `close = shutdown` to `STTEngine`.
  3. In `jarvis_main.py`, update shutdown handler to call `self.stt_engine.close()` safely.

### TASK 2: Integrate Live Hardware Telemetry into REST API
- **Target Files:**
  - `Network/API/rest_server.py` (Lines 62, 434)
- **Changes Required:**
  1. Import `HardwareMonitor` from `Desktop.Monitoring.hardware_monitor`.
  2. In `GET /api/v1/telemetry`, instantiate or fetch singleton `HardwareMonitor()` snapshot.
  3. Populate `cpu_percent`, `gpu_percent`, `vram_used_mb`, `vram_total_mb`, `gpu_temp_c`, `ram_used_gb`, `ram_total_gb` from `TelemetrySnapshot`.

### TASK 3: Fix Voice TTS Async Event Loop & Non-Blocking Playback
- **Target Files:**
  - `Voice/TTS/tts_engine.py` (Lines 341, 381)
- **Changes Required:**
  1. Replace `asyncio.run(_fetch())` in `_synthesize_edge_fallback` with event loop detection (`asyncio.get_event_loop()`).
  2. In `_playback_audio`, execute `winsound.PlaySound` with `winsound.SND_ASYNC` when `blocking=False` or wrap in a background thread to prevent UI thread lock.

### TASK 4: Enforce SecurityPermissionManager in DialogueManager Actions
- **Target Files:**
  - `Core/Conversation/dialogue_manager.py` (Lines 300-355)
- **Changes Required:**
  1. Import `get_automation_router` from `Automation.automation_router`.
  2. Modify `_execute_action(self, action)` to route `run_command`, `read_file`, `write_file`, `web_search` through `AutomationRouter.execute()`.

### TASK 5: Synchronize REST Voice Config Updates with Active Orchestrator
- **Target Files:**
  - `Network/API/rest_server.py` (Line 712)
  - `Voice/voice_orchestrator.py` (Line 238)
- **Changes Required:**
  1. When `POST /api/v1/voice/config` is called, update active `VoiceOrchestrator` state and `TTSEngine` parameters if initialized.


---
