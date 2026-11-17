# AI_CONTEXT.md — JARVIS / C.E.L.E.S.T.E. System Architecture & Technical Context

---

# 1. System Blueprint

### 1.1 Overview & System Identity
JARVIS (internally designated **C.E.L.E.S.T.E.** — *Cognitive Engineering & Logic Execution System for Tactical Engagements*) is an event-driven, hybrid local/cloud tactical assistant and desktop engineering control system. The system runs locally on Windows 11 x64 (AMD Ryzen 7 9700X, NVIDIA GeForce RTX 3080 10GB, 32GB RAM) and serves as an autonomous co-pilot with natural Persian and English voice interaction, full OS automation, hardware telemetry monitoring, and secure network remote control.

### 1.2 Tech Stack & Frameworks
- **Core Runtime & Language:** Python 3.10+ (Current runtime environment: Python 3.14 x64 on Windows).
- **Desktop UI Runtime:** .NET 8.0 WPF (Windows Presentation Foundation) with C# 12, XAML, hardware-accelerated 3D cosmic particle rendering, and custom Iron Man Holographic styling.
- **Web & Communication Framework:** FastAPI with Uvicorn server, supporting asynchronous REST API and duplex WebSockets on a single unified port (`8765`).
- **Local AI Inference Engine:** `llama.cpp` HTTP server binary (`llama-server.exe`) running CUDA-accelerated GGUF models (`Qwen 3.5 9B`, FlashAttention, `q8_0` KV cache, context window of 8192 tokens) on `http://127.0.0.1:8080/v1`.
- **Cloud AI Fallback / Hybrid Routing:** Google Gemini API (`gemini-2.5-flash`), Anthropic Claude API (`claude-3-7-sonnet-latest`), and OpenAI ChatGPT API (`gpt-4o`).
- **Speech-to-Text (STT):** `faster-whisper` (`Systran/faster-whisper-small` model, int8 quantization, CPU/CUDA execution).
- **Text-to-Speech (TTS):** Piper ONNX neural voice engine (`fa_IR-gyro-medium.onnx` at 16kHz/24kHz) with `edge-tts` cloud fallback.
- **Audio DSP:** NumPy, SciPy (pitch modulation, metallic comb filtering, convolution reverb).
- **Wake Word Detection:** PyAudio, sounddevice, RMS energy threshold gating, and sliding-window frame analysis.
- **Hardware Telemetry:** `psutil` (CPU, RAM, Disks), `pynvml` (NVIDIA GPU utilization, VRAM allocation, core clock, temperature).
- **Persistence & Storage:** SQLite3 database (`Data/Memory/jarvis_memory.db`) with Write-Ahead Logging (WAL) and Row-factory query mapping.

### 1.3 Primary Architectural Patterns
- **Multi-Process Hybrid Architecture:** Headless Python backend service running as a resilient background daemon alongside an independent .NET 8 WPF holographic client communicating over REST and WebSockets.
- **Single-Instance Mutex Enforcement:** Win32 Named Mutex (`Global\JARVIS_CORE_SINGLE_INSTANCE_MUTEX`) paired with socket port checks to guarantee a single daemon instance.
- **Shared-Engine Orchestration:** Memory-resident shared singleton instances of STT, WakeWord, TTS, and DSP managed by `VoiceOrchestrator` to prevent duplicate VRAM/RAM allocations.
- **4-Tier Security Gatekeeper:** Deterministic rule-based command inspection classifying every execution request into `SAFE`, `MODERATE`, `SENSITIVE`, or `DANGEROUS` prior to router dispatch.
- **LAN-Constrained Security Boundary:** Token-based bearer authentication combined with strict RFC 1918 private subnet validation (loopback and internal LAN IPs only).

---

# 2. Module & File Registry

### 2.1 Core & Daemon Orchestration
- `[Core/jarvis_main.py]` -> Master system orchestrator coordinating the 9-step initialization pipeline, mutex lifecycle, and Uvicorn runtime -> `JarvisMasterService`, `_acquire_single_instance_mutex()`.
- `[Core/jarvis_watchdog.py]` -> Out-of-process health supervisor executing periodic heartbeat pings against `/ping` and managing silent auto-restart cycles -> `run_watchdog(test_once: bool)`.
- `[Core/Config/config_loader.py]` -> Thread-safe central configuration manager loading, validating, and mutating `config.json` -> `ConfigManager`, `get_config()`, `load_config()`, `save_config()`.
- `[Core/Logger/logger.py]` -> Unified rotational file and console logging subsystem buffering recent log lines in-memory for API consumption -> `JarvisLogger`, `get_logger()`, `get_recent_logs()`.
- `[Installer/start_jarvis_hidden.vbs]` -> VBScript wrapper launching the Python daemon via `pythonw.exe` completely concealed without creating visible console windows.

### 2.2 Memory & Persistence
- `[Core/Memory/db_manager.py]` -> Low-level SQLite database connection manager handling WAL mode, schema initialization, table migrations, and row factories -> `DatabaseManager`, `get_db()`, `init_db()`.
- `[Core/Memory/memory_manager.py]` -> High-level memory controller managing conversational history, persistent factual memories, user preferences, and audit retention cleanup -> `MemoryManager`.
- `[Data/Memory/jarvis_memory.db]` -> Primary SQLite database store containing tables for conversations, facts, user preferences, and security audit logs.

### 2.3 AI Engine & LLM Routing
- `[Core/ModelManager/llamacpp_manager.py]` -> Process supervisor for `llama-server.exe` featuring VRAM adoption logic to avoid port 8080 collision -> `LlamaCppManager` (alias `ModelManager`).
- `[Core/AI/llm_client.py]` -> Hybrid local/cloud model router performing Jinja conversation template sanitization, provider auto-detection, and streaming response delivery -> `LLMClient`.
- `[Core/Conversation/dialogue_manager.py]` -> High-level dialogue orchestrator injecting persistent facts, parsing `<action>` and `<fact>` XML tags, managing web search triggers, and integrating with security routing -> `DialogueManager`, `parse_actions()`.

### 2.4 Voice Pipeline & DSP
- `[Voice/voice_orchestrator.py]` -> Central finite state machine (FSM) synchronizing wake word listening, speech transcription, dialogue generation, and audio synthesis -> `VoiceOrchestrator`, `VoiceState`.
- `[Voice/WakeWord/wakeword_engine.py]` -> Continuous audio stream listener computing RMS energy levels and switching between physical microphones and incoming network audio -> `WakeWordEngine`.
- `[Voice/STT/stt_engine.py]` -> Faster-Whisper speech-to-text inference engine processing 16kHz PCM audio buffers into Persian and English text -> `STTEngine`.
- `[Voice/TTS/tts_engine.py]` -> Neural text-to-speech synthesizer wrapping Piper ONNX, acoustic DSP post-processing, and fallback synthesis -> `TTSEngine`.
- `[Voice/AudioDSP/jarvis_dsp.py]` -> Acoustic DSP filter chain executing real-time pitch shifting, metallic comb filtering, and synthetic reverb convolution -> `JarvisAudioDSP` (alias `CelesteAudioDSP`).

### 2.5 Network & API Gateway
- `[Network/Authentication/auth_manager.py]` -> Security manager enforcing RFC 1918 LAN restrictions, Bearer token authentication, PIN-based pairing, and brute-force lockout -> `NetworkAuthManager`, `AuthResult`, `PairingResult`.
- `[Network/API/rest_server.py]` -> FastAPI application exposing REST routes for telemetry, memory CRUD, voice configuration, security confirmations, and chat -> `create_rest_app()`, `get_master_token()`, `validate_request_token()`.
- `[Network/WebSocket/ws_server.py]` -> Dual-endpoint WebSocket manager handling binary PCM stream ingestion on `/ws/lumia` and real-time JSON HUD telemetry on `/ws/desktop` -> `JarvisWebSocketManager`.

### 2.6 Automation, Security & OS Control
- `[Automation/automation_router.py]` -> Role-based command dispatcher executing validated operations across applications, filesystem, and system controls -> `AutomationRouter`, `Role`, `get_automation_router()`.
- `[Automation/Security/permission_manager.py]` -> 4-tier risk classification engine determining execution rights and managing asynchronous human confirmation queues -> `SecurityPermissionManager`, `PermissionLevel`, `PermissionResult`.
- `[Automation/Windows/system_control.py]` -> Operating system controller adjusting master audio volume, media playback, display brightness, and power states -> `SystemControl`, `SystemControlRisk`.
- `[Automation/Windows/startup_manager.py]` -> Windows auto-start manager manipulating HKCU Registry Run keys and Task Scheduler definitions -> `register_startup()`, `unregister_startup()`, `is_registered()`.
- `[Automation/Applications/app_manager.py]` -> Process lifecycle manager launching, monitoring, and terminating whitelisted desktop applications -> `AppManager`, `AppAction`.
- `[Automation/FileSystem/file_manager.py]` -> Filesystem abstraction layer performing sandboxed file search, read, write, directory listing, and sensitive file heuristic scans -> `FileManager`, `FileAction`, `SensitiveFileRisk`.
- `[Automation/Web/web_search.py]` -> Live online information retrieval engine aggregating results from Serper, Google Custom Search, and DuckDuckGo -> `WebSearchEngine`.

### 2.7 Hardware Telemetry & Desktop WPF UI
- `[Desktop/Monitoring/hardware_monitor.py]` -> Hardware telemetry daemon sampling CPU per-core load, RAM usage, storage volume capacities, and NVIDIA GPU metrics via NVML -> `HardwareMonitor`, `CpuMetrics`, `GpuMetrics`, `RamMetrics`, `DiskMetrics`, `TelemetrySnapshot`.
- `[Desktop/UI/App.xaml]` / `[Desktop/UI/App.xaml.cs]` -> WPF application entry point configuring global exception handling and application lifecycle.
- `[Desktop/UI/MainWindow.xaml]` / `[Desktop/UI/MainWindow.xaml.cs]` -> Holographic Iron Man HUD user interface featuring an interactive Arc Reactor, 3D cosmic star sphere, live Persian chat, audio visualizer, and management panels.
- `[Desktop/UI/Models/JarvisModels.cs]` -> C# data contracts and JSON serialization DTOs matching backend REST endpoints and WebSocket frames.
- `[Desktop/UI/Services/JarvisBackendClient.cs]` -> Asynchronous C# HTTP and WebSocket communication client handling auto-reconnection, token loading, and backend service calls -> `JarvisBackendClient`.

---

# 3. Data Schemas & Global State

### 3.1 Configuration Schema (`Configuration/config.json`)
```json
{
  "system": {
    "project_name": "string",
    "assistant_name": "string",
    "version": "string",
    "language": "string (e.g. 'fa')",
    "fallback_language": "string (e.g. 'en')",
    "single_instance_port": "int"
  },
  "llama_cpp": {
    "server_exe_path": "string",
    "model_gguf_path": "string",
    "target_model_name": "string",
    "host": "string",
    "port": "int",
    "base_url": "string",
    "health_url": "string",
    "n_gpu_layers": "int",
    "context_length": "int",
    "cpu_threads": "int",
    "flash_attn": "bool",
    "kv_cache_type": "string",
    "temperature": "float",
    "timeout_seconds": "int",
    "auto_launch_server": "bool"
  },
  "voice": {
    "wake_word": "string",
    "threshold": "float",
    "stt_model": "string",
    "stt_device": "string",
    "stt_compute_type": "string",
    "tts_model_path": "string",
    "sample_rate": "int",
    "speech_rate": "float",
    "dsp_pitch_semitones": "float",
    "dsp_metallic_mix": "float",
    "dsp_reverb_decay": "float"
  },
  "network": {
    "host": "string",
    "port": "int",
    "ws_port": "int",
    "require_auth": "bool",
    "token_file": "string",
    "heartbeat_interval": "int"
  },
  "memory": {
    "db_path": "string",
    "max_context_messages": "int",
    "retention_days": "int"
  },
  "security": {
    "require_confirmation_for_sensitive": "bool",
    "allow_powershell": "bool",
    "audit_log_path": "string"
  },
  "cloud_ai": {
    "enabled": "bool",
    "default_provider": "string",
    "proxy_url": "string",
    "gemini_api_key": "string",
    "gemini_model": "string",
    "claude_api_key": "string",
    "claude_model": "string",
    "openai_api_key": "string",
    "openai_model": "string"
  },
  "web_search": {
    "enabled": "bool",
    "provider": "string",
    "proxy_url": "string",
    "serper_api_key": "string",
    "google_api_key": "string",
    "google_cx": "string",
    "max_results": "int"
  }
}
```

### 3.2 Persistent Database Relational Schema (`Data/Memory/jarvis_memory.db`)
- **`conversations`:**
  - `id`: INTEGER PRIMARY KEY AUTOINCREMENT
  - `session_id`: TEXT NOT NULL
  - `role`: TEXT CHECK(role IN ('user', 'assistant', 'system'))
  - `content`: TEXT NOT NULL
  - `language`: TEXT DEFAULT 'fa'
  - `timestamp`: DATETIME DEFAULT CURRENT_TIMESTAMP
- **`facts`:**
  - `id`: INTEGER PRIMARY KEY AUTOINCREMENT
  - `category`: TEXT NOT NULL (e.g., 'user', 'system', 'preference')
  - `fact_key`: TEXT UNIQUE NOT NULL
  - `fact_value`: TEXT NOT NULL
  - `importance`: INTEGER DEFAULT 5 (1-10)
  - `created_at`: DATETIME DEFAULT CURRENT_TIMESTAMP
  - `updated_at`: DATETIME DEFAULT CURRENT_TIMESTAMP
- **`preferences`:**
  - `pref_key`: TEXT PRIMARY KEY
  - `pref_value`: TEXT NOT NULL
  - `updated_at`: DATETIME DEFAULT CURRENT_TIMESTAMP
- **`security_audit`:**
  - `id`: INTEGER PRIMARY KEY AUTOINCREMENT
  - `timestamp`: DATETIME DEFAULT CURRENT_TIMESTAMP
  - `command`: TEXT NOT NULL
  - `risk_level`: INTEGER NOT NULL (1=SAFE, 2=MODERATE, 3=SENSITIVE, 4=DANGEROUS)
  - `status`: TEXT NOT NULL ('GRANTED', 'DENIED', 'PENDING', 'TIMED_OUT')
  - `caller_role`: TEXT NOT NULL
  - `details`: TEXT

### 3.3 Core DTOs & Message Payloads

#### REST Chat
- **Request (`ChatRequest`):** `text: str`, `source: str = "desktop"`
- **Response (`ChatResponse`):** `spoken_response: str`, `response: Optional[str]`, `action: Optional[dict]`, `language: str`, `session_id: str`

#### REST Telemetry
- **Response (`TelemetryModel`):** `cpu_percent: float`, `gpu_percent: float`, `vram_used_mb: float`, `vram_total_mb: float`, `gpu_temp_c: float`, `ram_used_gb: float`, `ram_total_gb: float`

#### REST Voice Configuration
- **Payload (`VoiceConfigModel`):** `pitch_semitones: float`, `metallic_mix: float`, `reverb_decay: float`, `wake_threshold: float`, `muted: Optional[bool]`, `volume: Optional[int]`

#### REST Security Confirmations
- **Pending Item:** `action_id: str`, `command_type: str`, `risk_level: str`, `payload: dict`
- **Confirmation Request (`ConfirmActionRequest`):** `action_id: str`, `approved: bool`

#### WebSocket Frames
- **`/ws/lumia` Input:** Binary 16-bit 16kHz Mono PCM Audio Chunks OR JSON commands (`{"type": "heartbeat" | "mute" | "status"}`).
- **`/ws/desktop` Broadcast (`HudUpdateMessage`):**
  - `type`: `"hud_update"`
  - `state`: `"IDLE"` | `"LISTENING"` | `"THINKING"` | `"SPEAKING"` | `"MUTED"` | `"AWAITING_CONFIRMATION"`
  - `rms`: `float` (0.0 to 1.0)
  - `lumia_connected`: `bool`
  - `model_loaded`: `bool`
  - `muted`: `bool`
  - `active_mic`: `"mic"` | `"network"`
  - `telemetry`: `TelemetryModel` dict
  - `pending_confirmations`: `list` of active security requests
  - `last_message`: `dict` or `str` containing recent conversation text

### 3.4 Global Runtime State
- **`VoiceState` (Enum):** Tracks conversational flow: `IDLE` (monitoring audio), `LISTENING` (ingesting speech), `THINKING` (inferencing LLM/actions), `SPEAKING` (rendering audio), `MUTED` (pipeline paused), `AWAITING_CONFIRMATION` (awaiting human authorization).
- **`active_mic`:** Tracks active STT input source: switches to `"network"` when remote Lumia WebSocket connects; automatically falls back to `"mic"` (hardware soundcard) upon disconnection.
- **`pending_confirmations`:** Thread-safe in-memory dictionary storing sensitive pending actions with timestamps: `Dict[str, Tuple[datetime, str, Dict[str, Any]]]`.
- **`Single-Instance Mutex`:** Win32 Global kernel object `Global\JARVIS_CORE_SINGLE_INSTANCE_MUTEX`.

---

# 4. Data Flow & Communication

### 4.1 Voice Interaction Pipeline
```
[Physical Mic / Lumia Network Stream]
                  │
                  ▼
       [WakeWordEngine (RMS / Wake)]
                  │ (Wake word detected / Barge-in)
                  ▼
       [VoiceOrchestrator FSM -> LISTENING]
                  │
                  ▼
       [STTEngine (Faster-Whisper)] ──> Transcribed Persian Text
                  │
                  ▼
       [VoiceOrchestrator FSM -> THINKING]
                  │
                  ▼
       [DialogueManager] ──> Fetch SQLite Facts & Build Strict Jinja Prompt
                  │
                  ├──> Local LLM (llama-server.exe) OR Cloud Router (Gemini/Claude)
                  │
                  ▼
       Raw Response with <action> and <fact> tags
                  │
                  ├──> Fact Extractor: writes <fact> directly to SQLite `facts` table
                  ├──> Action Extractor: routes <action> to [AutomationRouter]
                  │         │
                  │         ▼
                  │    [SecurityPermissionManager]
                  │         ├── SAFE / MODERATE ──> Auto-Execute
                  │         └── SENSITIVE / DANGEROUS ──> Post to Confirmation Queue
                  │
                  ▼
       Cleaned Persian Spoken Text
                  │
                  ▼
       [VoiceOrchestrator FSM -> SPEAKING]
                  │
                  ▼
       [TTSEngine (Piper ONNX)] ──> Raw PCM Audio
                  │
                  ▼
       [JarvisAudioDSP] ──> Pitch Shift + Comb Filter + Convolution Reverb
                  │
                  ▼
       [Audio Playback (SoundDevice / Winsound)]
                  │
                  ▼
       [VoiceOrchestrator FSM -> IDLE]
```

### 4.2 Network & Remote Audio Stream Ingestion
1. **Connection & Authentication:** Client initiates WebSocket connection to `/ws/lumia` or `/ws/desktop`. Middleware verifies LAN RFC 1918 IP address and checks authorization Bearer token.
2. **Audio Streaming:** Lumia client streams raw 16kHz 16-bit mono PCM chunks.
3. **Dynamic Source Routing:** `JarvisWebSocketManager` sets `_active_mic = "network"` and invokes `VoiceOrchestrator.ingest_lumia_audio_chunk(pcm_bytes)`.
4. **Failover:** If Lumia client disconnects or times out (25 seconds heartbeat loss), `_active_mic` resets to `"mic"`, seamlessly reactivating the local soundcard without restarting the pipeline.

### 4.3 Desktop WPF HUD Telemetry & Control Loop
1. **Connection:** `JarvisBackendClient` loads local token and PIN from `Configuration/auth_token.json`, establishes a persistent WebSocket connection to `/ws/desktop`, and initiates background polling.
2. **Live State Synchronization:** `ws_server.py` broadcasts `hud_update` frames containing RMS audio levels, voice states, hardware telemetry, and pending security confirmations.
3. **UI Dispatching:** `MainWindow.xaml.cs` processes incoming frames on the UI thread, driving the Arc Reactor rotation speed, visualizer bars, and pending action dialogues.

---

# 5. Extension Points (Hooks)

### 5.1 Adding Automation Commands & Tool Capabilities
- **File:** `Automation/automation_router.py`
  - Add route prefix mappings to `ROUTE_TABLE` pointing to target subsystem methods.
  - Assign command permissions to the role matrix in `ROLE_CAPABILITIES` (`GUEST`, `USER`, or `ADMIN`).
- **File:** `Automation/Applications/app_manager.py`
  - Register new known desktop application aliases in `_KNOWN_APPS` dictionary.
- **File:** `Automation/Windows/system_control.py`
  - Expose new Win32 API system actions (e.g., monitor controls, networking adapters).

### 5.2 Enhancing Security Rules & Risk Classifications
- **File:** `Automation/Security/permission_manager.py`
  - Extend command pattern matching dictionaries: `_SAFE_PATTERNS`, `_MODERATE_PATTERNS`, `_SENSITIVE_PATTERNS`, and `_DANGEROUS_PATTERNS`.
  - Override `check_permission()` logic to integrate custom role checks or multi-factor authorization.

### 5.3 Integrating New AI Models & Cloud Providers
- **File:** `Core/AI/llm_client.py`
  - Add recognition patterns in `PROVIDER_KEYWORDS` for provider auto-detection.
  - Implement vendor client handlers in `_chat_cloud()` (e.g., adding Mistral, DeepSeek, or local Ollama).
  - Modify `_sanitize_messages()` for provider-specific chat template peculiarities.
- **File:** `Core/ModelManager/llamacpp_manager.py`
  - Adjust CLI arguments passed to `llama-server.exe` (e.g., FlashAttention, split-mode VRAM offloading).

### 5.4 Adding REST Endpoints & WebSocket Commands
- **File:** `Network/API/rest_server.py`
  - Register new FastAPI router paths under the `/api/v1` namespace using standard route decorators.
- **File:** `Network/WebSocket/ws_server.py`
  - Extend message handling inside `handle_desktop_connection()` or `handle_lumia_connection()` to support new JSON packet types.

### 5.5 Extending Memory Categories & RAG Capabilities
- **File:** `Core/Memory/memory_manager.py`
  - Add custom categorization or vector/semantic search methods alongside current SQLite routines.
- **File:** `Core/Conversation/dialogue_manager.py`
  - Customize `SYSTEM_PROMPT` to refine Tehrani Persian pronunciation guidelines or add specialized prompt blocks.
  - Add regex parsers for new XML tags (e.g., `<widget>`, `<reminder>`).

### 5.6 Expanding Desktop WPF User Interface
- **File:** `Desktop/UI/MainWindow.xaml` / `Desktop/UI/MainWindow.xaml.cs`
  - Add new `<TabItem>` components within the holographic management panel.
  - Wire control event handlers through `JarvisBackendClient`.
- **File:** `Desktop/UI/Models/JarvisModels.cs`
  - Define new C# classes and deserialization contracts matching any modified or newly introduced backend payloads.

---

# 6. Current State & TODOs

### 6.1 Architectural Notes & Completed Milestones
- **Multi-Instance Guard:** Named Mutex `Global\JARVIS_CORE_SINGLE_INSTANCE_MUTEX` is active and prevents duplicate core instances.
- **Single-Port Unification:** REST API and WebSockets operate on single unified port `8765`.
- **VRAM Adoption:** Master orchestrator detects pre-existing `llama-server.exe` on port `8080` and adopts it instead of launching duplicate instances.
- **Security Bypass Fixed:** `DialogueManager` `<action>` execution is routed through `AutomationRouter` and `SecurityPermissionManager` rather than executing raw subshell processes.
- **Jinja Template Fix:** `_sanitize_messages` enforces strictly alternating user/assistant turns starting at index 0 and ending with a user message.

### 6.2 Structural Gaps & Known TODOs
- **Telemetry Live Integration:** `Network/API/rest_server.py` endpoint `GET /api/v1/telemetry` historically returned mock data; integration with live snapshots from `Desktop/Monitoring/hardware_monitor.py` (`HardwareMonitor.get_telemetry_snapshot()`) is pending full cross-module hookup.
- **TTS Synchronous Playback:** In `Voice/TTS/tts_engine.py`, synchronous playback invocations (`winsound.PlaySound`) can block caller threads; requires full transition to background daemon worker threads for all audio playback.
- **Async Event Loop Fallback Collision:** `Voice/TTS/tts_engine.py` calls `asyncio.run()` in `_synthesize_edge_fallback()`; must be guarded against running inside an existing Uvicorn event loop thread.
- **MemoryManager Return Value Contract:** `Core/jarvis_main.py` evaluates `if not self.memory.load_settings():`, while `load_settings()` in `memory_manager.py` returns `None`, producing an erroneous non-fatal warning on startup.
- **STT Shutdown Method Alias:** `Core/jarvis_main.py` shutdown sequence checks for `close()`, whereas `STTEngine` in `Voice/STT/stt_engine.py` names its cleanup method `shutdown()`.
- **Lumia Mobile Client Implementation:** The backend WebSocket ingest layer (`/ws/lumia`) is fully implemented and tested; mobile client software (e.g., UWP/Windows Phone or modern mobile audio stream client) is external to this repository.
- **Memory Facts Fallback ID Resolution:** REST endpoint `DELETE /api/v1/memory/facts/{fact_key}` expects either a string key or integer ID; fallback in-memory dictionary lookup requires integer-to-string key normalization.
