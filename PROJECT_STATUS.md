# V.E.S.P.E.R. Project Status
**System:** V.E.S.P.E.R. (*Voice-Enabled Strategic Processing & Execution Resource* / `vesper` / `وِسپِر`)  
**Architecture:** Multi-Process Hybrid (Python 3.14 x64 Core Daemon + .NET 8 WPF Holographic HUD Client)  
**Target Runtime LLM:** Gemma-4-26B-A4B-IT (`gemma-4-26B-A4B-it-UD-IQ4_XS.gguf`) with hybrid RTX 3080 + Ryzen 7 9700X DDR5 MoE tensor offloading (`-ot`)  
**Voice Pipeline:** Dual-Engine TTS (Gemini Online TTS + Gooya Bozorg v1.5 Local TTS with Hybrid BF16/FP32+TF32, Dynamic Runway Pipelining, Gapless sounddevice Playback & Windows Process-Tree VRAM Release) + Shenava-Koochik-v1.0 (sherpa-onnx) + Silero VAD (CPU-only, 0 MB VRAM) STT + VesperAudioDSP  
**Unified Port:** `8765` (REST API & Duplex WebSockets at `/ws/lumia` and `/ws/desktop`)  
**Persistent Storage:** SQLite3 `Data/Memory/vesper_memory.db` with WAL mode & `user_facts`

---

## Completed & Verified Milestones

- **Stage 1.1 to 1.3 (Config, Logger, SQLite, MemoryManager):** COMPLETED & VERIFIED
- **Stage 2.1 to 2.2 (LlamaCppManager, LLMClient, DialogueManager):** COMPLETED & VERIFIED
- **Stage 3.1 to 3.4 (STT, TTS, DSP, WakeWord, VoiceOrchestrator):** COMPLETED & VERIFIED
- **Stage 4.1 to 4.3 (4-Tier Security, HardwareMonitor, AutomationRouter):** COMPLETED & VERIFIED
- **Stage 5.1 to 5.2 + Patch 5.2 (NetworkAuthManager with ipaddress & brute-force lockout, FastAPI REST Server on port 8765):** COMPLETED & VERIFIED
- **Stage 5.3 (VesperWebSocketManager at /ws/lumia & /ws/desktop, 16kHz PCM binary audio streaming, automatic mic failover network <-> mic):** COMPLETED & VERIFIED
- **Stage 6.1 (VesperMasterService 9-step orchestrator, Single-Instance Mutex, VRAM-safe LLM adoption & --dry-run):** COMPLETED & VERIFIED
- **Stage 6.2 (StartupManager HKCU Run & Task Scheduler, vesper_watchdog.py, start_vesper_hidden.vbs):** COMPLETED & VERIFIED
- **Stage 7.1 (Desktop WPF Foundation, Iron Man Holographic Theme, Desktop Client & Models):** COMPLETED & VERIFIED
- **Stage 7.2 (Desktop WPF Holographic HUD, Animated Concentric Arc Reactor, Live RMS Visualizer, Telemetry & RTL Persian Chat):** COMPLETED & VERIFIED
- **Stage 7.3 (Desktop WPF Management Tabs: Voice/DSP Config, SQLite Facts CRUD, Security Audit/Startup Toggle & Live Logs):** COMPLETED & VERIFIED

---

## System Audit & Bug Fix History

- **Bug Fix (2026-11-12):** `vesper_main.py` production path never started uvicorn on port 8765. Added `import uvicorn` + `uvicorn.run()` call in `_step9_ready_report()`. Also added health watchdog thread startup before the blocking `uvicorn.run()`.
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
  - Step 1: Upgraded `Configuration/config.json` and `Core/Config/config_loader.py` to unified v2.0.0 schema (VESPER, unified port 8765, dual-engine gemini/gooya, preserved cloud_ai/web_search/online_api_key keys).
  - Step 2: Updated `Core/ModelManager/llamacpp_manager.py` command builder to support Gemma-4-26B-A4B MoE with `-ngl 32`, `-c 8192`, `-b 2048`, `-ub 512`, `-t 8`, `-tb 8`, `-fa on`, `-ctk q8_0`, `-ctv q8_0`, and `-ot "blk\.(1[0-9]|[2-9][0-9])\.ffn_.*_exps\.=CPU"`, keeping port 8080 adoption logic intact.
  - Step 3: Fixed broken telemetry import in `Network/WebSocket/ws_server.py` to `Desktop.Monitoring.hardware_monitor.HardwareMonitor`, combining live CPU, GPU, VRAM, and RAM metrics into WebSocket `hud_update` broadcasts.
  - Step 4: Fixed 401 Unauthorized watchdog bug in `Core/vesper_watchdog.py` by switching health check URL to unauthenticated `/api/v1/health`.
  - Step 5: Cleaned up obsolete files `Core/AI/llm_client_new.py`, `Core/Conversation/_fix_llm.py`, `Network/Voice/`, `Voice/TTS/tts_engine.py.bak`, `Network/API/rest_server.py.bak`, and verified `config.json.bak` removal.
  - Step 6: Verified with non-blocking self-tests (`config_loader`, `llamacpp_manager`, `hardware_monitor`, `ws_server`, `rest_server`).

- **Backend Phase 2 (2026-11-18): Dual-Engine TTS (Gemini + Gooya Bozorg v1.5 with BF16/SDPA & VRAM Management) and STT CPU Lock** — COMPLETED & VERIFIED.
  - Step 1: Locked STT (`Voice/STT/stt_engine.py`) to CPU when `voice.stt_force_cpu` is true OR free VRAM < 3800 MB, configuring faster-whisper with `compute_type="int8"` and `cpu_threads=8` to eliminate VRAM contention with Gemma-4-26B-A4B and Gooya.
  - Step 2: Verified Gooya environment & 24kHz Persian audio reference (`Data/Models/gooya/speaker_reference.wav`). Built low-latency warm worker `Voice/TTS/gooya_worker.py` running in isolated Python 3.11 environment (`Data/Models/gooya/venv`) with PyTorch 2.6.0+cu124, Chatterbox 0.1.7, and S3Tokenizer.
  - Step 3: Rewrote `Voice/TTS/tts_engine.py` for strict dual-engine operation (`gemini` & `gooya`), removing all obsolete piper/pocket-tts/xtts/pyttsx3 branches.
  - Step 4: Exposed `update_config()` and live mode switching on `TTSEngine`, and verified all non-blocking self-tests pass in under 5 seconds.

- **Backend Phase 3 (2026-11-18): SQLite Memory Facts Integration & REST API Updates for Dual TTS and Config** — COMPLETED & VERIFIED.
  - Step 1: Upgraded `Core/Memory/memory_manager.py` with full SQLite backing:
    - Implemented `get_all_facts()` returning `List[Dict[str, Any]]` directly from SQLite `user_facts` table.
    - Implemented polymorphic `delete_fact(fact_id_or_key)` supporting both integer row `id` and string `fact_key` with immediate SQLite row deletion and existence verification.
    - Implemented `get_all_preferences()` returning `Dict[str, Any]` and `get_stats()` returning counts of conversations, facts, preferences, and audit logs.
    - Enforced thread-safe singleton pattern (`get_memory_manager()`) and enabled `check_same_thread=False` in `Core/Memory/db_manager.py` for FastAPI multithreaded database operations.
  - Step 2: Wired `Network/API/rest_server.py` to SQLite & Unified Voice Config:
    - Replaced hardcoded model name in `/api/v1/status` with live `llama_cpp.target_model_name` from `ConfigManager` (`"Gemma 4 26B-A4B IT (IQ4_XS)"`).
    - Directly connected `GET /api/v1/memory/facts`, `POST /api/v1/memory/facts`, and `DELETE /api/v1/memory/facts/{fact_key}` to SQLite via `MemoryManager`, completely removing reliance on `_facts_fallback_store`.
    - Updated `GET`, `POST`, and `PUT` `/api/v1/voice/config` to accept and return the full voice and DSP settings dictionary (`engine_mode`, `online_api_key`, `gemini_model`, `gemini_voice_name`, `gooya_model_dir`, `gooya_reference_audio`, `gooya_exaggeration`, `gooya_cfg_weight`, `gooya_precision`, `gooya_auto_unload`, `dsp_pitch_semitones`, `dsp_metallic_mix`, `dsp_reverb_mix`, `wake_threshold`).
    - Persisted settings changes to `Configuration/config.json` via `ConfigManager`, updated `VesperAudioDSP` and `WakeWordEngine` live, and called `tts_engine.update_config()` for instant mode switching (`gemini` <-> `gooya`) and dynamic VRAM unloading.
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
  - Step 1: Upgraded Desktop UI models with expanded `VoiceConfigModel` (engine_mode, online_api_key, gemini_model, gemini_voice_name, gooya_model_dir, gooya_reference_audio, gooya_exaggeration, gooya_cfg_weight, gooya_precision, gooya_auto_unload, dsp_pitch_semitones, dsp_metallic_mix, dsp_reverb_mix, wake_threshold) and `SystemStatusModel` target model name `Gemma 4 26B-A4B IT (IQ4_XS)`.
  - Step 2: Added `GetVoiceConfigAsync()` to Desktop UI backend client for querying `GET /api/v1/voice/config`.
  - Step 3: Upgraded `Desktop/UI/MainWindow.xaml` Tab 1 to "VOICE & TTS" with interactive engine switcher (`gemini` vs `gooya`), Gemini model & voice selectors, Gooya speaker prompt selector, exaggeration/CFG sliders, BF16/FP32 precision picker, auto-unload VRAM toggle, and mastering DSP sliders.
  - Step 4: Upgraded `Desktop/UI/MainWindow.xaml.cs` with `LoadVoiceConfigFromBackendAsync()`, dynamic panel visibility toggling, file browsing dialog, real-time slider labels, live config persistence, and voice testing.
  - Step 5: Compiled with `dotnet build Desktop/UI/` in Release (0 Warnings, 0 Errors). Staged and committed.

- **Backend Phase 6 (2026-11-20): Gooya Bozorg v1.5 Hybrid Stability, Dynamic Runway Pipelined TTS & VRAM Cap (Phase 1 & 1.5)** — COMPLETED & VERIFIED.
  - **Hybrid Precision & Offline Stability (`Voice/TTS/gooya_worker.py`):** Resolved `s3gen/flow.py` bf16 crash by casting only `t3` to `bfloat16` + SDPA while keeping `s3gen` and `ve` strictly in `float32` + TF32; enforced offline HF env (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `TQDM_DISABLE=1`), Windows child PID process-tree cleanup (`taskkill /T /F`), pre-cached 24kHz mono `speaker_reference_clean.wav`, startup CUDA kernel warmup, comma-preserving Persian normalization, and Windows-safe CUDA allocator fragmentation cap (`max_split_size_mb:128,garbage_collection_threshold:0.7`).
  - **Dynamic Runway Pipelined TTS & Gapless Audio (`Voice/TTS/tts_engine.py`, `Configuration/config.json`):** Implemented Producer-Consumer Dynamic Runway Scheduler using live playback runway + EWMA speed/RTF telemetry, sentence-first Persian/English segmentation (preserving decimals and keeping replies `<=115` chars single-shot), gapless continuous `sounddevice.OutputStream` playback (`<40ms` barge-in abort with `winsound` fallback), full-fidelity `gooya_cfm_timesteps=10`, and conditional VRAM cache trimming at `2450 MB`.
  - **Verified End-to-End Benchmarks (RTX 3080 alongside Gemma-4-26B):**
    - Peak VRAM reserved reduced from 2962.0 MB (shared-RAM spill risk) down to ~2154.0 MB (safely under 2.45 GB cap).
    - Long multi-sentence TTS TTFA reduced from 12.20s to 2.72s–4.15s (depending on opening sentence length) with stall=0.0ms across 28.76s continuous 3-chunk playback (RTF ~0.56–0.58, zero Gemini fallback).

- **Phase 2 (2026-11-22): Shenava-Koochik + Silero VAD CPU-Only STT Engine, Persian ITN & Config** — COMPLETED & VERIFIED.
  - **Persian ITN (`Voice/STT/persian_itn.py`):** Implemented Persian spoken number normalization converting number words (units, teens, tens, hundreds, thousands, millions, billions with grammatical conjunction 'و') to Persian/ASCII digits, preserving surrounding punctuation. Passed all unit test cases.
  - **Unified Configuration (`Configuration/config.json`, `Core/Config/config_loader.py`):** Added dedicated top-level `'stt'` configuration with `shenava_sherpa_onnx`, `provider: 'cpu'`, Silero VAD parameters, ITN flags, and audio device options. Updated default `stt_model_dir` to `Data/Models/stt/shenava`. Validated with 0 errors.
  - **CPU-Only STT Engine (`Voice/STT/stt_engine.py`, `Voice/STT/__init__.py`):** Completely rewrote STTEngine using `sherpa-onnx` OfflineRecognizer (FastConformer CTC) + Silero VAD executing 100% on CPU (0 MB GPU VRAM). Added non-hardcoded device resolution (`resolve_input_device`, `open_resilient_stream` with WASAPI mapping), FIFO VAD framing with zero-padding, thread safety with `_infer_lock`, audio preprocessing (mono conversion, resampling, optional gain and Butterworth highpass filter), and RTF telemetry.
  - **Verification:** Self-tests pass in under 3 seconds: 2s silence yields 0 STT latency; real Persian speech (`sample_fa.wav`, 8.35s) decodes cleanly in 56.8ms (RTF ~0.007). Backward-compatibility tests (`test_stt.py`) pass with 0 errors.

- **Phase 3 (2026-11-22): VoiceOrchestrator Integration, TTS Echo-Suppression, Test Suite & Legacy Cleanup** — COMPLETED & VERIFIED.
  - **VoiceOrchestrator Clean Constructor & Echo-Suppression (`Voice/voice_orchestrator.py`):**
    - Removed legacy Whisper constructor kwargs (`STTEngine()` default initialization).
    - Integrated dedicated `_tts_echo_suppressed` lock and state machine methods (`is_echo_suppressed()`, `pause_listening_for_tts()`, `resume_listening_after_tts(tail_cooldown_s=0.25)`).
    - Protected `ingest_lumia_audio_chunk()` against playback bleed during TTS.
    - Updated `barge_in()` and `unmute()` to automatically clear echo suppression and restore listening.
    - Wrapped voice turn TTS playback in `try ... finally` to guarantee listening is always restored even if speech throws or interrupts.
    - Expanded voice turn profiling with `vad_ms`, `stt_ms`, and `rtf`, logging unified metrics on turn completion.
  - **WebSocket TTS Echo Suppression (`Network/WebSocket/ws_server.py`):**
    - Integrated minimal surgical echo-suppression hook into `_trigger_tts_async()` (`_tts_play()`): pauses listening before TTS playback, awaits speech completion, and safely resumes listening with 250ms tail cooldown in `finally:` block without altering message schemas.
  - **Offline STT Test Suite (`Voice/STT/test_stt.py`):**
    - Replaced legacy test suite with 6 comprehensive automated non-blocking checks for Shenava-Koochik CTC + Silero VAD CPU-only engine: engine import and CPU provider check, Persian ITN normalization, 2s silence handling, real Persian WAV (`sample_fa.wav`) decoding via both file and PCM (RTF ~0.007), 48kHz to 16kHz resampling, and resilient device resolution. Cleanly closes engine and exits with code 0 in under 2 seconds.
  - **Requirements & Legacy Cleanup (`requirements.txt`, `AI_CONTEXT.md`, `PROJECT_STATUS.md`):**
    - Replaced `faster-whisper` with `sherpa-onnx` in `requirements.txt`.
    - Purged all stale references to `Faster-Whisper` / `Data/Models/whisper` across architecture docs and system status.
