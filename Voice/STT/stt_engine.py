"""JARVIS Voice - Offline Speech-to-Text Engine (Stage 3.1)"""
import logging
from pathlib import Path
from typing import Any, Dict, Optional
import numpy as np
from faster_whisper import WhisperModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger(__name__)

class STTEngine:
    SUPPORTED_LANGUAGES = {"fa", "en"}
    DEFAULT_MODEL = "small"
    DEFAULT_DEVICE = "cpu"
    DEFAULT_COMPUTE_TYPE = "int8"
    DEFAULT_CPU_THREADS = 8

    def __init__(self, model_size=None, device=None, compute_type=None, cpu_threads=None):
        self._model_size = model_size or self.DEFAULT_MODEL
        self._device = device or self.DEFAULT_DEVICE
        self._compute_type = compute_type or self.DEFAULT_COMPUTE_TYPE
        self._cpu_threads = cpu_threads or self.DEFAULT_CPU_THREADS
        self._model = None
        self._initialized = False

    def _detect_language(self, language=None):
        if language and language.lower() in self.SUPPORTED_LANGUAGES:
            return language.lower()
        if language:
            logger.warning("Unsupported language '%s', defaulting to 'en'", language)
        return "en"

    def _detect_device(self):
        try:
            from faster_whisper.utils import _check_cuda_available
            if _check_cuda_available():
                return "cuda"
        except Exception:
            pass
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except (ImportError, Exception):
            pass
        return "cpu"

    def _select_optimal_device(self):
        try:
            import pynvml
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            free_mb = info.free / (1024 * 1024)
            pynvml.nvmlShutdown()
            if free_mb >= 2400:
                return "cuda", "int8_float16", 8
        except Exception as exc:
            logger.debug("pynvml unavailable (%s), falling back to CPU", exc)
        return "cpu", "int8", 8

    def initialize(self):
        if self._initialized and self._model is not None:
            logger.info("STT engine already initialized")
            return True
        device = self._device
        if device == "auto":
            device = self._detect_device()
            logger.info("Auto-detected device: %s", device)
        compute_type = self._compute_type
        if device == "cuda" and compute_type == "int8":
            compute_type = "float16"
            logger.info("Switched compute_type to float16 for CUDA")
        try:
            self._model = WhisperModel(
                self._model_size, device=device, compute_type=compute_type,
                cpu_threads=self._cpu_threads,
                download_root=str(PROJECT_ROOT / "Data" / "Models" / "whisper"),
            )
            self._initialized = True
            logger.info("STT engine initialized successfully")
            return True
        except Exception as exc:
            logger.error("Failed to initialize STT engine: %s", exc)
            self._model = None
            return False

    def transcribe_pcm(self, pcm_bytes, sample_rate=16000, language=None):
        if not self._initialized:
            if not self.initialize():
                raise RuntimeError("STT engine not initialized")
        lang = self._detect_language(language)
        logger.debug("Transcribing PCM (%d bytes, %d Hz, lang=%s)", len(pcm_bytes), sample_rate, lang)
        pcm_array = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        import time
        t0 = time.time()
        segments, info = self._model.transcribe(
            pcm_array, language=lang if lang != "en" else None, task="transcribe",
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 500, "threshold": 0.5},
        )
        latency_ms = (time.time() - t0) * 1000
        text_parts = []
        segments_list = []
        for seg in segments:
            text_parts.append(seg.text.strip())
            segments_list.append({"start": round(seg.start, 3), "end": round(seg.end, 3),
                                  "text": seg.text.strip(), "confidence": round(seg.avg_logprob, 4)})
        result = {"text": " ".join(text_parts).strip(), "language": info.language,
                  "language_probability": round(info.language_probability, 4),
                  "segments": segments_list,
                  "duration": round(info.duration, 3) if hasattr(info, "duration") else 0.0,
                  "latency_ms": round(latency_ms, 2)}
        logger.info("Transcription complete: lang=%s, duration=%.2fs, text_len=%d",
                     result["language"], result["duration"], len(result["text"]))
        return result

    def shutdown(self):
        if self._model is not None:
            del self._model
            self._model = None
            self._initialized = False
            logger.info("STT engine shut down")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("=" * 60)
    print("JARVIS STT Engine - Self Test (Stage 3.1)")
    print("=" * 60)
    engine = STTEngine(model_size="small", device="cpu", compute_type="int8", cpu_threads=8)
    ok = engine.initialize()
    print(f"Initialization: {'OK' if ok else 'FAILED'}")
    silent_pcm = np.zeros(16000 * 2, dtype=np.int16).tobytes()
    try:
        result = engine.transcribe_pcm(silent_pcm, sample_rate=16000, language="en")
        print(f"Transcription (silence): '{result['text']}' | lang={result['language']}")
    except Exception as exc:
        print(f"Transcription error: {exc}")
    engine.shutdown()
    print("=" * 60)
    print("Self-test complete.")
    print("=" * 60)
