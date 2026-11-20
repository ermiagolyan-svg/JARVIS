# AI_CONTEXT.md — C.E.L.E.S.T.E. v2.0 Architecture & Technical Context

---

# 1. System Blueprint

### 1.1 Overview & System Identity
The system identity is **C.E.L.E.S.T.E. v2.0** (*Cognitive Engineering & Logic Execution System for Tactical Engagements*), formerly JARVIS. C.E.L.E.S.T.E. is an event-driven, hybrid local/cloud tactical assistant and desktop engineering control system. The system runs locally on Windows 11 x64 (AMD Ryzen 7 9700X 8-Core / 16-Thread Processor, NVIDIA GeForce RTX 3080 10GB VRAM, 32GB DDR5-6400 RAM) and serves as an autonomous co-pilot with natural Persian and English voice interaction, full OS automation, live hardware telemetry monitoring, and secure network remote control.

### 1.2 Tech Stack & Frameworks
- **Core Runtime & Language:** Python 3.10+ (Current runtime environment: Python 3.14 x64 on Windows).
- **Desktop UI Runtime:** .NET 8.0 WPF (Windows Presentation Foundation) with C# 12, XAML, hardware-accelerated 3D cosmic particle rendering, and custom Iron Man Holographic HUD styling.
- **Web & Communication Framework:** FastAPI with Uvicorn server, supporting asynchronous REST API and duplex WebSockets on a single unified port (`8765`).
- **Target Runtime LLM:** **Gemma-4-26B-A4B-IT (`gemma-4-26B-A4B-it-UD-IQ4_XS.gguf`)** running via CUDA-accelerated `llama-server.exe` on `http://127.0.0.1:8080/v1` (~31–38 tok/s, leaving ~2.5 GB VRAM free for Gooya):
  - **MoE Tensor Offloading (`-ot`):** Hybrid offloading across RTX 3080 (10GB VRAM) and Ryzen 7 9700X DDR5 RAM using regular expression:
    `-ot "blk\.([89]|[1-9][0-9])\..*exps=CPU"`
  - **Optimization:** FlashAttention (`-fa on`), 8-bit quantized KV cache (`-ctk q8_0 -ctv q8_0`), context window of 16,384 tokens, `enable_thinking: false` (`reasoning_budget: 0`), and compute threads `-t 8 -tb 12`.
  - **VRAM Adoption:** Master orchestrator detects pre-existing `llama-server.exe` on port 8080 and adopts it instead of launching duplicate instances.
- **Cloud AI Fallback / Hybrid Routing:** Google Gemini API (`gemini-2.5-flash`), Anthropic Claude API (`claude-3-7-sonnet-latest`), and OpenAI ChatGPT API (`gpt-4o`).
- **Speech-to-Text (STT):** `faster-whisper` (`Systran/faster-whisper-small` model, int8 quantization, locked to CPU with 8 threads when `voice.stt_force_cpu` is enabled or free VRAM < 3800MB to avoid VRAM contention with Gemma-4-26B and Gooya).
- **Voice Pipeline — Dual-Engine TTS:**
  1. **Gemini Online TTS (`gemini`):** Cloud neural voice (`gemini-2.5-flash-preview-tts`), selectable voices (`Kore`, `Puck`, `Fenrir`, `Aoede`), dedicated API key (`voice.online_api_key`), automatic Persian name normalization (`"C.E.L.E.S.T.E."` to `"سلست"`), 24kHz PCM decoding, and acoustic DSP processing.
  2. **Gooya Bozorg v1.5 Local TTS (`gooya`):** Local 801M Chatterbox architecture (`Reza2kn/Gooya-Bozorg-v1.5`) at `Data/Models/gooya` running in an isolated Python 3.11 subprocess worker (`Data/Models/gooya/venv`, `torch 2.6.0+cu124`, `chatterbox-tts 0.1.7`):
     - **Hybrid Precision & Stability:** Strictly offline (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `TQDM_DISABLE=1`), `T3` in `bfloat16` + PyTorch `sdpa` FlashAttention, `S3Gen` & `VoiceEncoder` strictly in `float32` + TF32 (preventing `s3gen/flow.py` bf16 token/length rounding crashes), full-fidelity `cfm_timesteps=10`, pre-cached clean 24kHz mono speaker reference (`Data/Models/gooya/speaker_reference_clean.wav`), and startup CUDA kernel warmup.
     - **Low-Latency Streaming & VRAM Control:** Producer-Consumer **Dynamic Runway Scheduler** (sentence-first Persian/English segmentation with live EWMA speed/runway sizing, single-shot bypass for replies `<=115` chars, comma-preserving Persian prosody), gapless `sounddevice.OutputStream` playback (`<40ms` barge-in abort with `winsound` fallback), Windows-safe CUDA allocator fragmentation cap (`max_split_size_mb:128,garbage_collection_threshold:0.7`) keeping `VRAM reserved ~2154 MB` (conditional trim at `2450 MB`), and full Windows child PID process-tree cleanup (`taskkill /T /F`) on `unload_gooya()`.
- **Audio DSP:** NumPy, SciPy via `CelesteAudioDSP` (real-time pitch modulation, metallic comb filtering, convolution reverb).
- **Wake Word Detection:** PyAudio, sounddevice, RMS energy threshold gating, and sliding-window frame analysis via `WakeWordEngine`.
- **Hardware Telemetry:** Direct NVML (`pynvml`), `nvidia-smi`, and `psutil` via `HardwareMonitor` (CPU per-core load, RAM usage, NVIDIA GPU utilization, VRAM allocation, temperature).
- **Persistence & Storage:** SQLite3 database (`Data/Memory/jarvis_memory.db`) with Write-Ahead Logging (WAL), multithreaded support (`check_same_thread=False`), storing `conversations`, `user_facts`, `preferences`, and `security_audit`.

### 1.3 Primary Architectural Patterns
- **Multi-Process Hybrid Architecture:** Headless Python backend service running as a resilient background daemon alongside an independent .NET 8 WPF holographic client communicating over REST and WebSockets on port `8765`.
- **Single-Instance Mutex Enforcement:** Win32 Named Mutex (`Global\JARVIS_CORE_SINGLE_INSTANCE_MUTEX`) paired with socket port checks to guarantee a single daemon instance.
- **Shared-Engine Orchestration:** Memory-resident shared singleton instances of STT, WakeWord, TTS, and DSP managed by `VoiceOrchestrator` to prevent duplicate VRAM/RAM allocations.
- **Dynamic VRAM Lifecycle Management:** Automatic unloading of local Gooya weights (with full Windows child process-tree termination) upon mode switch to Gemini or after inference (`gooya_auto_unload`), returning 100% of GPU memory to Gemma-4-26B MoE.
- **4-Tier Security Gatekeeper:** Deterministic rule-based command inspection classifying execution requests into `SAFE`, `MODERATE`, `SENSITIVE`, or `DANGEROUS` prior to router dispatch.
- **LAN-Constrained Security Boundary:** Token-based bearer authentication combined with strict RFC 1918 private subnet validation (loopback and internal LAN IPs only).

---

# 2. Module & File Registry

### 2.1 Core & Daemon Orchestration
- `[Core/jarvis_main.py]` -> Master system orchestrator coordinating the 9-step initialization pipeline, mutex lifecycle, and Uvicorn runtime on port 8765 -> `JarvisMasterService`, `_acquire_single_instance_mutex()`.
- `[Core/jarvis_watchdog.py]` -> Out-of-process health supervisor executing periodic heartbeat pings against unauthenticated `/api/v1/health` and managing silent auto-restart cycles -> `run_watchdog(test_once: bool)`.
- `[Core/Config/config_loader.py]` -> Thread-safe central configuration manager loading, validating, and mutating `Configuration/config.json` -> `ConfigManager`, `get_config()`, `load_config()`, `save_config()`.
- `[Core/Logger/logger.py]` -> Unified rotational file and console logging subsystem with `turn_id` correlation, subprocess stream pumping, and in-memory ring buffer for API consumption -> `JarvisLogger`, `get_logger()`, `get_recent_logs()`, `turn_context()`.
- `[Installer/start_jarvis_hidden.vbs]` -> VBScript wrapper launching the Python daemon via `pythonw.exe` completely concealed without creating visible console windows.

### 2.2 Memory & Persistence
- `[Core/Memory/db_manager.py]` -> Low-level SQLite database connection manager handling WAL mode, schema initialization, multithreading (`check_same_thread=False`), table migrations, and row factories -> `DatabaseManager`, `get_db()`, `init_db()`.
- `[Core/Memory/memory_manager.py]` -> High-level memory controller managing conversational history, persistent factual memories in SQLite `user_facts`, user preferences, and audit retention cleanup -> `MemoryManager`, `get_all_facts()`, `set_fact()`, `delete_fact()`, `get_stats()`.
- `[Data/Memory/jarvis_memory.db]` -> Primary SQLite database store containing tables for `conversations`, `user_facts`, `preferences`, and `security_audit`.

### 2.3 AI Engine & LLM Routing
- `[Core/ModelManager/llamacpp_manager.py]` -> Process supervisor for `llama-server.exe` featuring Gemma-4-26B-A4B MoE command building (`-ot`, `-fa on`, `-ctk q8_0 -ctv q8_0`) and VRAM port 8080 adoption logic -> `LlamaCppManager` (alias `ModelManager`).
- `[Core/AI/llm_client.py]` -> Hybrid local/cloud model router performing Jinja conversation template sanitization, provider auto-detection, and streaming response delivery -> `LLMClient`.
- `[Core/Conversation/dialogue_manager.py]` -> High-level dialogue orchestrator injecting persistent SQLite facts, parsing `<action>` and `<fact>` XML tags, routing automation actions through `AutomationRouter` and `SecurityPermissionManager`, and handling web search triggers -> `DialogueManager`.

### 2.4 Voice Pipeline & DSP
- `[Voice/voice_orchestrator.py]` -> Central finite state machine (FSM) synchronizing wake word listening, speech transcription, dialogue generation, and audio synthesis -> `VoiceOrchestrator`, `VoiceState`.
- `[Voice/WakeWord/wakeword_engine.py]` -> Continuous audio stream listener computing RMS energy levels and switching between physical microphones and incoming network audio -> `WakeWordEngine`.
- `[Voice/STT/stt_engine.py]` -> Faster-Whisper speech-to-text inference engine locked to CPU with 8 threads when `voice.stt_force_cpu` is enabled or free VRAM < 3800MB -> `STTEngine`.
- `[Voice/TTS/tts_engine.py]` -> Dual-engine neural TTS router (Gemini Online + Gooya Bozorg v1.5) featuring Dynamic Runway Producer-Consumer pipelining, sentence-first Persian/English chunking, gapless `sounddevice` continuous audio playback (`_ContinuousAudioPlayer` + `winsound` fallback), instant `stop_speaking` barge-in, clean Gemini fallback, and Windows process-tree `unload_gooya()` -> `TTSEngine`, `_ContinuousAudioPlayer`.
- `[Voice/TTS/gooya_worker.py]` -> Isolated Python 3.11 warm CUDA worker running Gooya Bozorg v1.5 in strictly offline hybrid precision (`t3` in `bf16` + SDPA, `s3gen` & `ve` in `fp32` + TF32), cached fp32 speaker conditioning, startup CUDA kernel warmup, comma-preserving Persian normalization, adaptive `cfm_timesteps`, and VRAM peak trimming (<2.45 GB) -> `GooyaWorker`.
- `[Voice/AudioDSP/jarvis_dsp.py]` -> Acoustic DSP filter chain executing real-time pitch shifting, metallic comb filtering, and synthetic reverb convolution -> `CelesteAudioDSP` (alias `JarvisAudioDSP`).

### 2.5 Network & API Gateway
- `[Network/Authentication/auth_manager.py]` -> Security manager enforcing RFC 1918 LAN restrictions, Bearer token authentication, PIN-based pairing, and brute-force lockout -> `NetworkAuthManager`, `AuthResult`, `PairingResult`.
- `[Network/API/rest_server.py]` -> FastAPI application on unified port `8765` exposing REST routes for telemetry, SQLite memory facts CRUD, dual-engine voice configuration, security confirmations, unified logs, and chat -> `create_rest_app()`, `get_master_token()`, `validate_request_token()`.
- `[Network/WebSocket/ws_server.py]` -> Dual-endpoint WebSocket manager on unified port `8765` handling binary PCM stream ingestion on `/ws/lumia` and real-time JSON HUD telemetry on `/ws/desktop` -> `JarvisWebSocketManager`.

### 2.6 Automation, Security & OS Control
- `[Automation/automation_router.py]` -> Role-based command dispatcher executing validated operations across applications, filesystem, and system controls -> `AutomationRouter`, `Role`, `get_automation_router()`.
- `[Automation/Security/permission_manager.py]` -> 4-tier risk classification engine (`SAFE`, `MODERATE`, `SENSITIVE`, `DANGEROUS`) determining execution rights and managing asynchronous human confirmation queues -> `SecurityPermissionManager`, `PermissionLevel`, `PermissionResult`.
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

### 3.1 Configuration Schema (`Configuration/config.json` v2.0)
```json
{
  "system": {
    "project_name": "CELESTE",
    "assistant_name": "C.E.L.E.S.T.E.",
    "version": "2.0.0",
    "language": "fa-IR",
    "log_level": "INFO",
    "fallback_language": "en",
    "single_instance_port": 8764
  },
  "llama_cpp": {
    "server_binary_path": "D:/AI/llama.cpp/llama-server.exe",
    "server_exe_path": "D:/AI/llama.cpp/llama-server.exe",
    "model_gguf_path": "D:/AI/Models/Gemma-4-26B-A4B-IQ4_XS/gemma-4-26B-A4B-it-UD-IQ4_XS.gguf",
    "target_model_name": "Gemma 4 26B-A4B IT (IQ4_XS)",
    "host": "127.0.0.1",
    "port": 8080,
    "base_url": "http://127.0.0.1:8080/v1",
    "health_url": "http://127.0.0.1:8080/health",
    "context_length": 16384,
    "gpu_layers": 99,
    "threads": 8,
    "threads_batch": 12,
    "batch_size": 2048,
    "ubatch_size": 512,
    "flash_attn": true,
    "cache_type_k": "q8_0",
    "cache_type_v": "q8_0",
    "override_tensor_pattern": "blk\\.([89]|[1-9][0-9])\\..*exps=CPU",
    "temperature": 0.6,
    "top_p": 0.95,
    "timeout_seconds": 60,
    "auto_launch_server": true,
    "parallel": 1,
    "load_mode": "none",
    "jinja": true,
    "reasoning_budget": 0,
    "enable_thinking": false
  },
  "voice": {
    "engine_mode": "gooya",
    "sample_rate": 24000,
    "online_api_key": "<GEMINI_API_KEY>",
    "gemini_model": "gemini-2.5-flash-preview-tts",
    "gemini_voice_name": "Kore",
    "gooya_model_dir": "Data/Models/gooya",
    "gooya_reference_audio": "D:\\AI\\JARVIS\\Data\\Models\\gooya\\speaker_reference_clean.wav",
    "gooya_exaggeration": 0.6,
    "gooya_cfg_weight": 0.7,
    "gooya_precision": "bf16",
    "gooya_auto_unload": false,
    "gooya_chunking_enabled": true,
    "gooya_single_shot_max_chars": 115,
    "gooya_first_sentence_max_chars": 105,
    "gooya_first_chunk_min_chars": 42,
    "gooya_chunk_max_chars": 150,
    "gooya_chunk_min_chars": 45,
    "gooya_runway_safety_s": 0.4,
    "gooya_cfm_timesteps": 10,
    "gooya_vram_trim_mb": 2450,
    "gooya_warmup_on_load": true,
    "gooya_trim_silence": true,
    "gooya_use_continuous_audio": true,
    "stt_model_dir": "Data/Models/whisper",
    "stt_force_cpu": true,
    "wake_word": "hey_jarvis",
    "wake_threshold": 0.15,
    "dsp_pitch_semitones": 0.0,
    "dsp_metallic_mix": 0.0,
    "dsp_reverb_mix": 0.0,
    "pitch_semitones": 0.0
  },
  "network": {
    "host": "0.0.0.0",
    "port": 8765,
    "rest_port": 8765,
    "ws_port": 8765,
    "require_auth": true,
    "token_file": "Configuration/auth_token.json",
    "heartbeat_interval": 10
  },
  "memory": {
    "db_path": "Data/Memory/jarvis_memory.db",
    "max_context_messages": 20,
    "retention_days": 365
  },
  "security": {
    "require_confirmation_for_sensitive": true,
    "allow_powershell": true,
    "audit_log_path": "Logs/security_audit.log"
  },
  "cloud_ai": {
    "enabled": false,
    "default_provider": "gemini",
    "gemini_model": "gemini-2.5-flash"
  },
  "web_search": {
    "enabled": true,
    "provider": "auto",
    "max_results": 4
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
- **`user_facts`:**
  - `id`: INTEGER PRIMARY KEY AUTOINCREMENT
  - `category`: TEXT NOT NULL (e.g., 'user', 'system', 'preference')
  - `key`: TEXT UNIQUE NOT NULL
  - `value`: TEXT NOT NULL
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

---

# 4. Data Flow & Communication

### 4.1 Voice Interaction Pipeline
```
[Physical Mic / Lumia Network Stream (/ws/lumia)]
                  │
                  ▼
       [WakeWordEngine (RMS / Wake Threshold)]
                  │ (Wake word detected / Barge-in)
                  ▼
       [VoiceOrchestrator FSM -> LISTENING]
                  │
                  ▼
       [STTEngine (Faster-Whisper on CPU)] ──> Transcribed Persian Text
                  │
                  ▼
       [VoiceOrchestrator FSM -> THINKING]
                  │
                  ▼
       [DialogueManager] ──> Fetch SQLite `user_facts` & Build Jinja Prompt
                  │
                  ├──> Local LLM (Gemma-4-26B-A4B via llama-server -ot) OR Cloud Router
                  │
                  ▼
       Raw Response with <action> and <fact> tags
                  │
                  ├──> Fact Extractor: writes <fact> directly to SQLite `user_facts`
                  ├──> Action Extractor: routes <action> to [AutomationRouter]
                  │         │
                  │         ▼
                  │    [SecurityPermissionManager]
                  │         ├── SAFE / MODERATE ──> Auto-Execute
                  │         └── SENSITIVE / DANGEROUS ──> Post to Confirmation Queue
                  │
                  ▼
       Cleaned Persian Spoken Text ("C.E.L.E.S.T.E." -> "سلست")
                  │
                  ▼
       [VoiceOrchestrator FSM -> SPEAKING]
                  │
                  ▼
       [TTSEngine Dual-Engine Router]
            ├── Gemini Online TTS (gemini-2.5-flash-preview-tts via REST)
            └── Gooya Bozorg v1.5 (Hybrid BF16 T3 + FP32/TF32 S3Gen + Dynamic Runway Pipelining)
                  │
                  ▼
       [CelesteAudioDSP] ──> Pitch Shift + Comb Filter + Convolution Reverb
                  │
                  ▼
       [Gapless Continuous Playback (sounddevice OutputStream + winsound fallback, <40ms barge-in)]
                  │
                  ▼
       [VoiceOrchestrator FSM -> IDLE]
```

### 4.2 Network & Remote Audio Stream Ingestion
1. **Connection & Authentication:** Client initiates WebSocket connection to `/ws/lumia` or `/ws/desktop` on unified port `8765`. Middleware verifies LAN RFC 1918 IP address and checks authorization Bearer token.
2. **Audio Streaming:** Lumia client streams raw 16kHz 16-bit mono PCM chunks.
3. **Dynamic Source Routing:** `JarvisWebSocketManager` sets `active_mic = "network"` and invokes `VoiceOrchestrator.ingest_lumia_audio_chunk(pcm_bytes)`.
4. **Failover:** If Lumia client disconnects or times out (25 seconds heartbeat loss), `active_mic` resets to `"mic"`, seamlessly reactivating the local soundcard without restarting the pipeline.

### 4.3 Desktop WPF HUD Telemetry & Control Loop
1. **Connection:** `JarvisBackendClient` loads local token and PIN from `Configuration/auth_token.json`, establishes a persistent WebSocket connection to `/ws/desktop` on port `8765`.
2. **Live State Synchronization:** `ws_server.py` broadcasts `hud_update` frames containing RMS audio levels, voice states, live hardware telemetry (sampled via `HardwareMonitor`), and pending security confirmations.
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
  - Adjust CLI arguments passed to `llama-server.exe` (e.g., MoE tensor offloading `-ot`, FlashAttention `-fa on`).

### 5.4 Adding REST Endpoints & WebSocket Commands
- **File:** `Network/API/rest_server.py`
  - Register new FastAPI router paths under the `/api/v1` namespace using standard route decorators.
- **File:** `Network/WebSocket/ws_server.py`
  - Extend message handling inside `handle_desktop_connection()` or `handle_lumia_connection()` to support new JSON packet types.

### 5.5 Extending Memory Categories & RAG Capabilities
- **File:** `Core/Memory/memory_manager.py`
  - Add custom categorization or vector/semantic search methods alongside current SQLite `user_facts` routines.
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

# 6. Current State & Completed Milestones

### 6.1 Architectural Milestones Completed
- **Multi-Instance Guard:** Named Mutex `Global\JARVIS_CORE_SINGLE_INSTANCE_MUTEX` is active and prevents duplicate core instances.
- **Single-Port Unification:** REST API and WebSockets operate on single unified port `8765`.
- **Target Runtime LLM:** Gemma-4-26B-A4B-IT (`gemma-4-26B-A4B-it-UD-IQ4_XS.gguf`) integrated with MoE tensor offloading (`-ot`), FlashAttention, 16k Q8 KV cache, and automatic port 8080 adoption.
- **Dual-Engine TTS + Phase 1.5 Low-Latency Gooya Pipeline:** Fully operational Gemini Online TTS + Gooya Bozorg v1.5 Local TTS with hybrid BF16/FP32+TF32 stability, offline execution, pre-cached 24kHz clean speaker reference, Dynamic Runway Producer-Consumer chunking (stall=0.0ms, VRAM reserved ~2154 MB), gapless `sounddevice` playback, and Windows process-tree VRAM release.
- **SQLite Memory Facts:** Full CRUD integration for `user_facts` via `MemoryManager` and REST endpoints.
- **Security Bypass Fixed:** `DialogueManager` `<action>` execution is routed through `AutomationRouter` and `SecurityPermissionManager` rather than executing raw subshell processes.
- **Jinja Template Fix:** `_sanitize_messages` enforces strictly alternating user/assistant turns starting at index 0 and ending with a user message.

### 6.2 Verified Audit Fixes & Resolved Items
All structural gaps identified during previous system audits have been resolved and verified:
1. **Telemetry Live Integration:** `Network/API/rest_server.py` endpoint `GET /api/v1/telemetry` and WebSocket `hud_update` frames are connected to `Desktop.Monitoring.hardware_monitor.HardwareMonitor`, providing real-time CPU, GPU, VRAM, and RAM metrics.
2. **TTS Non-Blocking Playback & Async Event Loop:** `Voice/TTS/tts_engine.py` executes audio synthesis and gapless streaming playback in background worker threads, eliminating caller blocking and avoiding Uvicorn async event loop collisions.
3. **MemoryManager Return Value Contract:** Startup methods in `Core/Memory/memory_manager.py` return boolean status (`True`), resolving initialization checks in `Core/jarvis_main.py`.
4. **STT Shutdown Method Alias:** `STTEngine` exposes `close()` as an alias to `shutdown()`, ensuring clean daemon termination.
5. **Memory Facts Fallback ID Resolution:** REST endpoint `DELETE /api/v1/memory/facts/{fact_id_or_key}` supports both integer SQLite primary keys and string keys with direct database deletion.
6. **Watchdog Unauthenticated Health Check:** `Core/jarvis_watchdog.py` queries `/api/v1/health` instead of token-protected endpoints, preventing false 401 restarts.
7. **Gooya BF16 S3Gen Crash & VRAM Spill Fixed:** Isolated `t3` to `bfloat16` while keeping `s3gen`/`ve` in `float32` + TF32, added Windows-safe CUDA allocator cap + Dynamic Runway chunking (VRAM reserved reduced from 2962 MB to ~2154 MB, stall=0.0ms, zero fallback).
