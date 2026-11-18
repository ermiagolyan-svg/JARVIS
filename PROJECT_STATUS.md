# C.E.L.E.S.T.E. v2.0 Project Status
**System:** C.E.L.E.S.T.E. v2.0 (*Cognitive Engineering & Logic Execution System for Tactical Engagements*)  
**Architecture:** Multi-Process Hybrid (Python 3.14 x64 Core Daemon + .NET 8 WPF Holographic HUD Client)  
**Target Runtime LLM:** Gemma-4-26B-A4B-IT (`gemma-4-26B-A4B-it-UD-IQ4_XS.gguf`) with hybrid RTX 3080 + Ryzen 7 9700X DDR5 MoE tensor offloading (`-ot`)  
**Voice Pipeline:** Dual-Engine TTS (Gemini Online TTS + Gooya Bozorg v1.5 Local TTS with BF16/SDPA & Dynamic VRAM Unloading) + Faster-Whisper STT (CPU Lock) + CelesteAudioDSP  
**Unified Port:** `8765` (REST API & Duplex WebSockets at `/ws/lumia` and `/ws/desktop`)  
**Persistent Storage:** SQLite3 `Data/Memory/jarvis_memory.db` with WAL mode & `user_facts`

---

## Completed & Verified Milestones

- **Stage 1.1 to 1.3 (Config, Logger, SQLite, MemoryManager):** COMPLETED & VERIFIED
- **Stage 2.1 to 2.2 (LlamaCppManager, LLMClient, DialogueManager):** COMPLETED & VERIFIED
- **Stage 3.1 to 3.4 (STT, TTS, DSP, WakeWord, VoiceOrchestrator):** COMPLETED & VERIFIED
- **Stage 4.1 to 4.3 (4-Tier Security, HardwareMonitor, AutomationRouter):** COMPLETED & VERIFIED
- **Stage 5.1 to 5.2 + Patch 5.2 (NetworkAuthManager with ipaddress & brute-force lockout, FastAPI REST Server on port 8765):** COMPLETED & VERIFIED
- **Stage 5.3 (JarvisWebSocketManager at /ws/lumia & /ws/desktop, 16kHz PCM binary audio streaming, automatic mic failover network <-> mic):** COMPLETED & VERIFIED
- **Stage 6.1 (JarvisMasterService 9-step orchestrator, Single-Instance Mutex, VRAM-safe LLM adoption & --dry-run):** COMPLETED & VERIFIED
- **Stage 6.2 (StartupManager HKCU Run & Task Scheduler, jarvis_watchdog.py, start_jarvis_hidden.vbs):** COMPLETED & VERIFIED
- **Stage 7.1 (Desktop WPF Foundation, Iron Man Holographic Theme, JarvisModels & JarvisBackendClient):** COMPLETED & VERIFIED
- **Stage 7.2 (Desktop WPF Holographic HUD, Animated Concentric Arc Reactor, Live RMS Visualizer, Telemetry & RTL Persian Chat):** COMPLETED & VERIFIED
- **Stage 7.3 (Desktop WPF Management Tabs: Voice/DSP Config, SQLite Facts CRUD, Security Audit/Startup Toggle & Live Logs):** COMPLETED & VERIFIED

---

## System Audit & Bug Fix History

- **Bug Fix (2026-11-12):** `jarvis_main.py` production path never started uvicorn on port 8765. Added `import uvicorn` + `uvicorn.run()` call in `_step9_ready_report()`. Also added health watchdog thread startup before the blocking `uvicorn.run()`.
- **Task 2 (2026-11-14): LLM Client Message Sanitization & DialogueManager Action Security Enforcement** — COMPLETED & VERIFIED.
  - `Core/AI/llm_client.py`: Fixed `_sanitize_messages` fallback so that when only system messages are passed, the system content is used as a user turn (or `"Hello"` if empty), guaranteeing the returned list always starts with at most one `system` message (index 0), alternates `user`/`assistant`, and ends with a `user` message for `llama-server.exe` Jinja template compatibility.
  - `Core/Conversation/dialogue_manager.py`: Imported `get_automation_router`; replaced raw `subprocess.run(cmd, shell=True)` and direct file I/O in `_execute_action` with permission-checked routing through `router.check_permission(...)` + `router.execute(...)`. Denied actions now return a clear Persian denial message (`دسترسی رد شد: ...`). `web_search` remains unchanged via `get_web_search_engine()`.
- **System Audit & Bug Fixes (2026-11-17):** 5-Step Prioritized Fix Plan from FULL_SYSTEM_AUDIT COMPLETED & VERIFIED:
  - Task 1: Fixed Core Service & Memory Manager Return Mismatches (`stt_engine.close` alias, `memory_manager` boolean returns).
  - Task 2: Integrated Live Hardware Telemetry into REST API via `HardwareMonitor`.
  - Task 3: Fixed Voice TTS Async Event Loop & Non-Blocking Playback (`winsound.SND_ASYNC` / Threading).
  - Task 4: Enforced `SecurityPermissionManager` in `DialogueManager` Actions (Routed through `AutomationRouter`).
  - Task 5: Synchronized REST Voice Config Updates with Active Orchestrator.

---

## Backend Evolution (v2.0 Architecture)

- **Backend Phase 1 (2026-11-18): Config Consolidation, Gemma-4-26B-A4B MoE Offloading, Critical Bug Fixes & Cleanup** — COMPLETED & VERIFIED.
  - Step 1: Upgraded `Configuration/config.json` and `Core/Config/config_loader.py` to unified v2.0.0 schema (CELESTE, unified port 8765, dual-engine gemini/gooya, preserved cloud_ai/web_search/online_api_key keys).
  - Step 2: Updated `Core/ModelManager/llamacpp_manager.py` command builder to support Gemma-4-26B-A4B MoE with `-ngl 32`, `-c 8192`, `-b 2048`, `-ub 512`, `-t 8`, `-tb 8`, `-fa on`, `-ctk q8_0`, `-ctv q8_0`, and `-ot "blk\.(1[0-9]|[2-9][0-9])\.ffn_.*_exps\.=CPU"`, keeping port 8080 adoption logic intact.
  - Step 3: Fixed broken telemetry import in `Network/WebSocket/ws_server.py` to `Desktop.Monitoring.hardware_monitor.HardwareMonitor`, combining live CPU, GPU, VRAM, and RAM metrics into WebSocket `hud_update` broadcasts.
  - Step 4: Fixed 401 Unauthorized watchdog bug in `Core/jarvis_watchdog.py` by switching health check URL to unauthenticated `/api/v1/health`.
  - Step 5: Cleaned up obsolete files `Core/AI/llm_client_new.py`, `Core/Conversation/_fix_llm.py`, `Network/Voice/`, `Voice/TTS/tts_engine.py.bak`, `Network/API/rest_server.py.bak`, and verified `config.json.bak` removal.
  - Step 6: Verified with non-blocking self-tests (`config_loader`, `llamacpp_manager`, `hardware_monitor`, `ws_server`, `rest_server`).

- **Backend Phase 2 (2026-11-18): Dual-Engine TTS (Gemini + Gooya Bozorg v1.5 with BF16/SDPA & VRAM Management) and STT CPU Lock** — COMPLETED & VERIFIED.
  - Step 1: Locked STT (`Voice/STT/stt_engine.py`) to CPU when `voice.stt_force_cpu` is `true` OR free VRAM < 3800 MB, configuring `faster-whisper` with `compute_type="int8"` and `cpu_threads=8` to eliminate VRAM contention with Gemma-4-26B-A4B and Gooya.
  - Step 2: Verified Gooya environment & 24kHz Persian audio reference (`Data/Models/gooya/speaker_reference.wav`). Built low-latency warm worker `Voice/TTS/gooya_worker.py` running in isolated Python 3.11 environment (`Data/Models/gooya/venv`) with PyTorch 2.6.0+cu124, Chatterbox 0.1.7, and S3Tokenizer.
  - Step 3: Rewrote `Voice/TTS/tts_engine.py` for strict dual-engine operation (`gemini` & `gooya`), removing all obsolete piper/pocket-tts/xtts/pyttsx3 branches:
    - Gemini Online: Generates via Gemini TTS REST API (`gemini-2.5-flash-preview-tts`), normalizes `"C.E.L.E.S.T.E."` to `"سلست"`, decodes 24kHz PCM, and processes through `CelesteAudioDSP`.
    - Gooya Local: PyTorch SDPA FlashAttention enabled on T3, mixed-precision `bf16` via CUDA autocast, in-memory speaker embedding caching for prompt WAVs, and generation parameters (`exaggeration`, `cfg_weight`).
    - Dynamic VRAM Management: `unload_gooya()` completely shuts down/unloads the Gooya worker, triggers GC and CUDA cache release, immediately freeing 100% of VRAM back to the RTX 3080 when switched to Gemini or when `gooya_auto_unload` is true.
  - Step 4: Exposed `update_config()` and live mode switching on `TTSEngine`, and verified all non-blocking self-tests pass in under 5 seconds.

- **Backend Phase 3 (2026-11-18): SQLite Memory Facts Integration & REST API Updates for Dual TTS and Config** — COMPLETED & VERIFIED.
  - Step 1: Upgraded `Core/Memory/memory_manager.py` with full SQLite backing:
    - Implemented `get_all_facts(category=None)` and alias `list_facts()`, querying `user_facts` in SQLite and returning dictionaries containing keys matching backend and C# DTO expectations (`id`, `category`, `key`, `fact_key`, `value`, `fact_value`, `importance`, `updated_at`).
    - Implemented polymorphic `delete_fact(identifier)` supporting numeric `id`, integer string, or string `fact_key` with immediate SQLite row deletion and existence verification.
    - Implemented `get_all_preferences()` returning `Dict[str, Any]` and `get_stats()` returning counts of conversations, facts, preferences, and audit logs.
    - Enforced thread-safe singleton pattern (`get_memory_manager()`) and enabled `check_same_thread=False` in `Core/Memory/db_manager.py` for FastAPI multithreaded database operations.
  - Step 2: Wired `Network/API/rest_server.py` to SQLite & Unified Voice Config:
    - Replaced hardcoded model name in `/api/v1/status` with live `llama_cpp.target_model_name` from `ConfigManager` (`"Gemma 4 26B-A4B IT (IQ4_XS)"`).
    - Directly connected `GET /api/v1/memory/facts`, `POST /api/v1/memory/facts`, and `DELETE /api/v1/memory/facts/{fact_key}` to SQLite via `MemoryManager`, completely removing reliance on `_facts_fallback_store`.
    - Updated `GET`, `POST`, and `PUT` `/api/v1/voice/config` to accept and return the full voice and DSP settings dictionary (`engine_mode`, `online_api_key`, `gemini_model`, `gemini_voice_name`, `gooya_model_dir`, `gooya_reference_audio`, `gooya_exaggeration`, `gooya_cfg_weight`, `gooya_precision`, `gooya_auto_unload`, `dsp_pitch_semitones`, `dsp_metallic_mix`, `dsp_reverb_mix`, `wake_threshold`).
    - Persisted settings changes to `Configuration/config.json` via `ConfigManager`, updated `CelesteAudioDSP` and `WakeWordEngine` live, and called `tts_engine.update_config()` for instant mode switching (`gemini` <-> `gooya`) and dynamic VRAM unloading.
  - Step 3: Verified with non-blocking self-tests:
    - `test_memory.py`: 100% passed with exit code 0.
    - `Network.API.rest_server`: 18/18 assertions passed with exit code 0.

- **Backend Phase 4 (2026-11-18): Synchronize Project Documentation** — COMPLETED & VERIFIED.
  - Step 1: Updated `Documentation/API_CONTRACT.md` to reflect full v2.0 REST & WebSocket contracts:
    - Documented unified port `8765` for all REST and WebSocket channels (`/ws/lumia` and `/ws/desktop`).
    - Detailed binary 16kHz PCM streaming and dynamic mic failover on `/ws/lumia`.
    - Detailed `hud_update` frame schema with live `HardwareMonitor` metrics and state broadcast on `/ws/desktop`.
    - Updated `GET`, `POST`, and `PUT` `/api/v1/voice/config` payloads to document the Dual-Engine TTS schema (`gemini` and `gooya` parameters + DSP parameters) and dynamic VRAM unloading behavior.
    - Documented `GET /api/v1/memory/facts`, `POST /api/v1/memory/facts`, and polymorphic `DELETE /api/v1/memory/facts/{fact_id_or_key}` backed by SQLite `user_facts`.
    - Documented both `POST /api/v1/security/confirm` (body-based) and `POST /api/v1/security/audit/{item_id}/confirm` (path-based) confirmation endpoints.
  - Step 2: Updated `AI_CONTEXT.md`:
    - Updated system identity to **C.E.L.E.S.T.E. v2.0** (*Cognitive Engineering & Logic Execution System for Tactical Engagements*).
    - Updated Target Runtime LLM to **Gemma-4-26B-A4B-IT (`gemma-4-26B-A4B-it-UD-IQ4_XS.gguf`)** with hybrid RTX 3080 + Ryzen 7 9700X DDR5 MoE tensor offloading (`-ot`).
    - Updated Voice Pipeline specification to **Dual-Engine TTS**: Gemini Online TTS (`gemini-2.5-flash-preview-tts`) + Gooya Bozorg v1.5 Local TTS (Chatterbox, PyTorch SDPA, BF16, cached speaker reference, dynamic VRAM release) with Faster-Whisper STT CPU lock.
    - Updated configuration schema to reflect v2.0 `config.json`.
    - Recorded all historical audit items and structural gaps as verified and resolved.
  - Step 3: Updated `PROJECT_STATUS.md` with complete v2.0 tracking and synchronization.

- **Frontend Phase 5 (2026-11-19): Upgrade WPF Voice & TTS UI for Dual-Engine (Gemini + Gooya v1.5) & VRAM Controls** — COMPLETED & VERIFIED.
  - Step 1: Upgraded `Desktop/UI/Models/JarvisModels.cs` with expanded `VoiceConfigModel` (engine_mode, online_api_key, gemini_model, gemini_voice_name, gooya_model_dir, gooya_reference_audio, gooya_exaggeration, gooya_cfg_weight, gooya_precision, gooya_auto_unload, dsp_pitch_semitones, dsp_metallic_mix, dsp_reverb_mix, wake_threshold) and `SystemStatusModel` target model name `Gemma 4 26B-A4B IT (IQ4_XS)`.
  - Step 2: Added `GetVoiceConfigAsync()` to `Desktop/UI/Services/JarvisBackendClient.cs` for querying `GET /api/v1/voice/config`.
  - Step 3: Upgraded `Desktop/UI/MainWindow.xaml` Tab 1 to "VOICE & TTS" with interactive engine switcher (`gemini` vs `gooya`), Gemini model & voice selectors, Gooya speaker prompt selector, exaggeration/CFG sliders, BF16/FP32 precision picker, auto-unload VRAM toggle, and mastering DSP sliders.
  - Step 4: Upgraded `Desktop/UI/MainWindow.xaml.cs` with `LoadVoiceConfigFromBackendAsync()`, dynamic panel visibility toggling, file browsing dialog, real-time slider labels, live config persistence, and voice testing.
  - Step 5: Compiled with `dotnet build Desktop/UI/JarvisDesktop.csproj -c Release` (0 Warnings, 0 Errors). Staged and committed.
