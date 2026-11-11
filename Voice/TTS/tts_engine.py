"""JARVIS Voice - Offline Text-to-Speech Engine (Stage 3.2)."""
import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger(__name__)


class TTSEngine:
    """Offline TTS with piper-tts (Persian .onnx) and SAPI5 fallback."""

    DEFAULT_MODEL_NAME = "fa"
    SPEAK_QUEUE_MAX = 10
    SUPPORTED_LANGUAGES = {"fa", "en"}
    DEFAULT_SAMPLE_RATE = 22050

    def __init__(self, model_name: Optional[str] = None):
        self._model_name = model_name or self.DEFAULT_MODEL_NAME
        self._piper_available = False
        self._sapi5_available = False
        self._piper = None
        self._sapi_engine = None
        self._speaking = False
        self._lock = threading.Lock()
        self._queue = []
        logger.info("TTSEngine initialized (model=%s)", self._model_name)

    def initialize(self) -> bool:
        """Attempt to initialize piper-tts, then SAPI5 fallback."""
        try:
            from piper import PiperVoice
            model_path = PROJECT_ROOT / "Data" / "Models" / "piper" / f"{self._model_name}.onnx"
            if not model_path.exists():
                logger.warning("Piper model not found at %s", model_path)
                self._piper_available = False
            else:
                self._piper = PiperVoice.load(str(model_path))
                self._piper_available = True
                logger.info("piper-tts loaded: %s", model_path)
        except ImportError:
            logger.warning("piper-tts not installed; using SAPI5 fallback")
            self._piper_available = False

        if not self._piper_available:
            try:
                import pyttsx3
                self._sapi_engine = pyttsx3.init()
                voices = self._sapi_engine.getProperty("voices")
                if voices:
                    self._sapi5_available = True
                    logger.info("SAPI5 engine ready (%d voices)", len(voices))
                else:
                    logger.warning("No SAPI5 voices found")
            except ImportError:
                logger.error("Neither piper-tts nor pyttsx3 available")
                return False
        return True

    def speak(self, text: str, blocking: bool = False) -> bool:
        """Queue and play text through JARVIS DSP pipeline."""
        if not self._piper_available and not self._sapi5_available:
            logger.error("TTSEngine not initialized")
            return False
        with self._lock:
            self._queue.append(text)
            if len(self._queue) > self.SPEAK_QUEUE_MAX:
                self._queue.pop(0)
        if blocking:
            return self._speak_blocking(text)
        else:
            t = threading.Thread(target=self._speak_blocking, args=(text,), daemon=True)
            t.start()
            return True

    def stop_speaking(self) -> None:
        """Stop current speech playback."""
        with self._lock:
            self._speaking = False
        if self._sapi_engine:
            try:
                self._sapi_engine.stop()
            except Exception:
                pass
        logger.info("Speech stopped")

    def get_status(self) -> dict:
        """Return engine status info."""
        return {
            "piper_available": self._piper_available,
            "sapi5_available": self._sapi5_available,
            "model_name": self._model_name,
            "speaking": self._speaking,
            "queue_size": len(self._queue),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _speak_blocking(self, text: str) -> bool:
        """Synthesize and play audio (blocking)."""
        with self._lock:
            self._speaking = True
        try:
            if self._piper_available:
                return self._speak_piper(text)
            elif self._sapi5_available:
                return self._speak_sapi5(text)
        except Exception as exc:
            logger.error("Speech failed: %s", exc)
            with self._lock:
                self._speaking = False
            return False

    def _speak_piper(self, text: str) -> bool:
        """Synthesize via piper-tts and apply JARVIS DSP."""
        from Voice.AudioDSP.jarvis_dsp import JarvisAudioDSP
        dsp = JarvisAudioDSP()
        pcm = self._piper.synthesize(text)
        if pcm is None or len(pcm) == 0:
            logger.warning("piper returned empty audio for: %s", text[:50])
            return False
        pcm = (pcm / 32768.0).astype(np.float32)
        processed = dsp.process(pcm)
        self._playback_audio(processed)
        return True

    def _speak_sapi5(self, text: str) -> bool:
        """Synthesize via Windows SAPI5."""
        try:
            self._sapi_engine.say(text)
            self._sapi_engine.runAndWait()
            logger.info("SAPI5 speech queued")
            return True
        except Exception as exc:
            logger.error("SAPI5 speech failed: %s", exc)
            return False

    def _playback_audio(self, pcm: np.ndarray) -> None:
        """Play PCM via sounddevice (non-blocking)."""
        try:
            import sounddevice as sd
            sr = self.DEFAULT_SAMPLE_RATE
            if len(pcm) > 0 and np.max(np.abs(pcm)) > 0:
                if abs(pcm.dtype) != np.float32:
                    pcm = pcm.astype(np.float32)
                sd.play(pcm, samplerate=sr, blocking=False)
                logger.debug("Playback queued (%d samples @ %d Hz)", len(pcm), sr)
        except ImportError:
            logger.warning("sounddevice not available; audio not played")
        finally:
            with self._lock:
                self._speaking = False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("=" * 60)
    print("JARVIS TTSEngine - Self Test (Stage 3.2)")
    print("=" * 60)

    tts = TTSEngine(model_name="fa")
    ok = tts.initialize()
    print(f"Initialization: {'OK' if ok else 'FAILED'}")
    print(f"Status: {tts.get_status()}")

    try:
        from Voice.AudioDSP.jarvis_dsp import JarvisAudioDSP
        dsp = JarvisAudioDSP()
        t = np.linspace(0, 1.0, int(dsp._sr), dtype=np.float32)
        test_audio = 0.5 * np.sin(2 * np.pi * 440 * t).astype(np.float32)
        processed = dsp.process(test_audio)
        print(f"Mock audio: {len(test_audio)} -> {len(processed)} samples (DSP OK)")
        rms_in = np.sqrt(np.mean(test_audio**2))
        rms_out = np.sqrt(np.mean(processed**2))
        print(f"RMS ratio: {rms_out / rms_in:.4f}")
    except Exception as exc:
        print(f"DSP test error: {exc}")

    print("=" * 60)
    print("TTS self-test complete.")
    print("=" * 60)
