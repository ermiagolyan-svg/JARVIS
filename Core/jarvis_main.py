import sys
import os

# Add project root to sys.path so relative-style imports work when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signal
import signal
import argparse
import ctypes
import logging
import threading
import time
import uvicorn

from fastapi.testclient import TestClient

# --- Windows Single-Instance Mutex ---
_MUTEX_NAME = r"Global\JARVIS_CORE_SINGLE_INSTANCE_MUTEX"

def _acquire_single_instance_mutex() -> bool:
    """Acquire a named mutex to prevent duplicate JARVIS instances.
    Returns True if this instance owns the mutex (first instance).
    """
    handle = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if handle == 0:
        return False
    err = ctypes.windll.kernel32.GetLastError()
    if err == 183:
        # ERROR_ALREADY_EXISTS - another instance is running
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    return True


# --- Module Imports (exact signatures as provided) ---
from Core.Config.config_loader import ConfigManager
from Core.Logger.logger import get_logger
from Core.Memory.memory_manager import MemoryManager
from Core.ModelManager.llamacpp_manager import LlamaCppManager
from Voice.STT.stt_engine import STTEngine
from Voice.WakeWord.wakeword_engine import WakeWordEngine
from Voice.TTS.tts_engine import TTSEngine
from Voice.AudioDSP.jarvis_dsp import JarvisAudioDSP
from Voice.voice_orchestrator import VoiceOrchestrator
from Network.API.rest_server import create_rest_app


class JarvisMasterService:
    """Main JARVIS service orchestrator for Core/jarvis_main.py (Stage 6.1)."""

    def __init__(self, dry_run: bool = False):
        self._dry_run = dry_run
        self._spawned_llm: bool = False
        self._shutdown_event = threading.Event()
        self._llm_server_thread = None

        # Core components
        self.config: ConfigManager = None
        self.logger: logging.Logger = None
        self.memory: MemoryManager = None
        self.llm_manager: LlamaCppManager = None
        self.voice_orchestrator: VoiceOrchestrator = None
        self.stt_engine: STTEngine = None
        self.wake_word_engine: WakeWordEngine = None
        self.tts_engine: TTSEngine = None
        self.dsp: JarvisAudioDSP = None
        self.app = None

    # --- Step 1: Core Initialization ---
    def _init_core(self) -> bool:
        """Step 1: Init ConfigManager, Logger, and MemoryManager."""
        try:
            self.config = ConfigManager.get_instance()
            if not self.config.load():
                self.logger = get_logger('jarvis_main')
                self.logger.error("Failed to load configuration")
                return False

            self.logger = get_logger('jarvis_main')
            self.logger.info("Core initialization started")

            self.memory = MemoryManager()
            if not self.memory.load_settings():
                self.logger.warning("Memory settings could not be loaded (non-fatal)")

            self.logger.info("Step 1 complete: Core components initialized")
            return True
        except Exception as e:
            self.logger.error(f"Core initialization failed: {e}")
            return False

    # --- Steps 2 & 3: LLM Server with VRAM Protection ---
    def _init_llm_server(self) -> bool:
        """Steps 2 & 3: Init LlamaCppManager with VRAM protection.
        If port 8080 is already running, ADOPT it (do NOT spawn a second server).
        Only call start_server() if nothing is running and not dry_run.
        """
        try:
            self.llm_manager = LlamaCppManager()

            if self.llm_manager.is_server_running():
                # CRITICAL: Server already on port 8080 - ADOPT it
                self._spawned_llm = False
                self.logger.info(
                    "LLM server already running on port 8080 - ADOPTING (VRAM protection)"
                )
            else:
                # Only spawn if not dry_run and nothing is running
                if not self._dry_run:
                    if self.llm_manager.start_server():
                        self._spawned_llm = True
                        self.logger.info("LLM server started successfully")
                    else:
                        self.logger.error("Failed to start LLM server")
                        return False
                else:
                    self._spawned_llm = False
                    self.logger.info(
                        "[DRY-RUN] Skipped LLM server start (no llamacpp_manager)"
                    )

            return True
        except Exception as e:
            self.logger.error(f"LLM server init failed: {e}")
            return False

    # --- Step 4: Verify Model Ready ---
    def _verify_model_ready(self) -> bool:
        """Step 4: Call verify_model_ready(timeout=10)."""
        try:
            if self._spawned_llm or self.llm_manager.is_server_running():
                ready = self.llm_manager.verify_model_ready(timeout_seconds=10)
                if ready:
                    self.logger.info("LLM model verified ready")
                else:
                    self.logger.warning("Model not ready within timeout (non-fatal)")
                return ready
            return True  # dry_run or no server to verify
        except Exception as e:
            self.logger.error(f"Model verification failed: {e}")
            return False

    # --- Steps 5, 6, 7: Voice Components ---
    def _init_voice(self) -> bool:
        """Steps 5, 6, 7: Init VoiceOrchestrator with shared engines.
        STTEngine, WakeWordEngine, and TTSEngine are shared (NOT loaded twice).
        Ensures ingest_lumia_audio_chunk is wired to wake_word_engine.feed_audio.
        """
        try:
            # Create shared engines first
            self.stt_engine = STTEngine()
            self.wake_word_engine = WakeWordEngine()
            self.tts_engine = TTSEngine()
            self.dsp = JarvisAudioDSP()

            # Create VoiceOrchestrator (engines injected as attributes)
            self.voice_orchestrator = VoiceOrchestrator()
            self.voice_orchestrator.stt_engine = self.stt_engine
            self.voice_orchestrator.wake_word_engine = self.wake_word_engine
            self.voice_orchestrator.tts_engine = self.tts_engine

            # CRITICAL: Wire ingest_lumia_audio_chunk if not already defined
            if not hasattr(self.voice_orchestrator, 'ingest_lumia_audio_chunk'):
                def _ingest_lumia(pcm_bytes):
                    self.wake_word_engine.feed_audio(pcm_bytes)
                self.voice_orchestrator.ingest_lumia_audio_chunk = _ingest_lumia

            # Step 5: Initialize STT
            if not self.stt_engine.initialize():
                self.logger.error("STT engine initialization failed")
                return False
            self.logger.info("Step 5 complete: STT engine initialized")

            # Step 6: Initialize WakeWord
            if not self.wake_word_engine.initialize():
                self.logger.error("WakeWord engine initialization failed")
                return False
            self.logger.info("Step 6 complete: WakeWord engine initialized")

            # Step 7: Initialize TTS + DSP
            if not self.tts_engine.initialize():
                self.logger.error("TTS engine initialization failed")
                return False
            self.logger.info("Step 7 complete: TTS + DSP initialized")

            return True
        except Exception as e:
            self.logger.error(f"Voice init failed: {e}")
            return False

    # --- Step 8: REST API App ---
    def _init_rest_app(self) -> bool:
        """Step 8: Create REST app and attach voice_orchestrator."""
        try:
            self.app = create_rest_app()
            self.app.state.voice_orchestrator = self.voice_orchestrator
            self.logger.info("Step 8 complete: REST app created with voice_orchestrator")
            return True
        except Exception as e:
            self.logger.error(f"REST app init failed: {e}")
            return False

    # --- Step 9: Ready Report / Uvicorn Startup ---
    def _step9_ready_report(self) -> None:
        """Step 9: Print readiness report (dry_run) or start uvicorn (production)."""
        if self._dry_run:
            self.logger.info("=" * 60)
            self.logger.info("JARVIS Dry-Run Readiness Report")
            self.logger.info("=" * 60)
            steps = [
                ("1. Core", "Config + Logger + Memory"),
                ("2. LLM Server", "VRAM-protected spawn/adopt"),
                ("3. Model Verify", "verify_model_ready(timeout=10)"),
                ("4. Voice STT", "STTEngine initialized"),
                ("5. Voice WakeWord", "WakeWordEngine initialized"),
                ("6. Voice TTS", "TTSEngine + DSP initialized"),
                ("7. REST App", "FastAPI app with voice_orchestrator"),
                ("8. Uvicorn", "port 8765 (production only)"),
                ("9. Health Watchdog", "every 10s (production only)"),
            ]
            for num, desc in steps:
                self.logger.info(f"  Step {num}: {desc}")
            self.logger.info("=" * 60)
            self.logger.info("DRY-RUN COMPLETE — no server started")
            sys.exit(0)
        else:
            # Production: print TTS readiness message then start uvicorn server
            if self.tts_engine and hasattr(self.tts_engine, 'is_ready'):
                ready = self.tts_engine.is_ready()
            else:
                ready = False
            if ready:
                self.logger.info("سیستم جارویس آماده به کار است")
            else:
                self.logger.warning("TTS not yet ready — health watchdog will retry")

            # Start the REST API server on port 8765
            host = self.config.get("network", "host", "0.0.0.0")
            port = self.config.get("network", "port", 8765)
            self.logger.info(f"Starting REST API server on {host}:{port}")

            # Start health watchdog thread BEFORE uvicorn (uvicorn.run is blocking)
            if hasattr(self, '_health_watchdog') and callable(getattr(self, '_health_watchdog')):
                watchdog_thread = threading.Thread(target=self._health_watchdog, args=(10,), daemon=True)
                watchdog_thread.start()
                self.logger.info("Health watchdog thread started")

            uvicorn.run(self.app, host=host, port=port, log_level="info")

    # --- Initialize Pipeline ---
    def initialize(self) -> bool:
        """Execute the full 9-step initialization pipeline."""
        try:
            if not self._init_core():
                return False
            if not self._init_llm_server():
                return False
            if not self._verify_model_ready():
                return False
            if not self._init_voice():
                return False
            if not self._init_rest_app():
                return False

            # Step 9: ready report / uvicorn
            self._step9_ready_report()
            return True
        except Exception as e:
            self.logger.error(f"Initialization pipeline failed: {e}")
            return False

    # --- Shutdown / Signal Handlers ---
    def _shutdown_handler(self, signum, frame):
        """Handle SIGINT/SIGTERM for graceful shutdown."""
        self.logger.info("Shutdown signal received — stopping services...")
        self._shutdown_event.set()

        if self.voice_orchestrator:
            try:
                self.voice_orchestrator.stop()
            except Exception as e:
                self.logger.error(f"Failed to stop voice orchestrator: {e}")

        if self.stt_engine and hasattr(self.stt_engine, 'close'):
            try:
                self.stt_engine.close()
            except Exception as e:
                self.logger.error(f"Failed to close STT engine: {e}")

        # ONLY stop managed server if WE spawned it
        if self._spawned_llm and hasattr(self.llm_manager, 'stop_managed_server'):
            try:
                self.llm_manager.stop_managed_server()
                self.logger.info("Managed LLM server stopped")
            except Exception as e:
                self.logger.error(f"Failed to stop managed LLM server: {e}")

        if self._llm_server_thread and self._llm_server_thread.is_alive():
            self._llm_server_thread.join(timeout=5)
            self.logger.info("LLM server thread joined")

        self.logger.info("JARVIS shutdown complete")
        sys.exit(0)

    # --- Health Watchdog ---
    def _health_watchdog(self, interval: int = 10) -> None:
        """Periodic health check every `interval` seconds."""
        while not self._shutdown_event.is_set():
            try:
                if hasattr(self, 'app') and self.app is not None:
                    client = TestClient(self.app)
                    resp = client.get("/api/v1/health")
                    if resp.status_code != 200:
                        self.logger.warning(f"Health check failed: {resp.status_code}")
                else:
                    self.logger.debug("Health watchdog: app not yet available")
            except Exception as e:
                self.logger.debug(f"Health watchdog error: {e}")
            self._shutdown_event.wait(interval)

    # --- Main Block ---
    def run(self) -> None:
        """Entry point: initialize and start the service."""
        if not _acquire_single_instance_mutex():
            print("Another JARVIS instance is already running. Exiting.")
            sys.exit(1)

        try:
            self.initialize()
        except Exception as e:
            print(f"FATAL: {e}")
            sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JARVIS MasterService")
    parser.add_argument("--dry-run", action="store_true", help="Run readiness check only")
    args = parser.parse_args()

    service = JarvisMasterService(dry_run=args.dry_run)

    # Register signal handlers
    signal.signal(signal.SIGINT, service._shutdown_handler)
    signal.signal(signal.SIGTERM, service._shutdown_handler)

    service.run()
