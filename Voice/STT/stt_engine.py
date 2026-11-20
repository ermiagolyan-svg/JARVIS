"""C.E.L.E.S.T.E. Voice - Offline Speech-to-Text Engine (faster-whisper)
Unified Observability: Accurate full-generator inference timing, RTF profiling,
automatic turn_id inheritance, and full exception tracebacks.
"""
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
from faster_whisper import WhisperModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Core.Logger.logger import get_logger, setup_unified_logging

setup_unified_logging()
logger = get_logger("stt_engine")

CONFIG_PATH = PROJECT_ROOT / "Configuration" / "config.json"


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
        except Exception as exc:
            logger.debug("faster_whisper cuda check failed: %s", exc)
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except Exception as exc:
            logger.debug("torch cuda check failed: %s", exc)
        return "cpu"

    def _select_optimal_device(self) -> Tuple[str, str, int]:
        stt_force_cpu = True
        try:
            if CONFIG_PATH.exists():
                cfg_data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                stt_force_cpu = cfg_data.get("voice", {}).get("stt_force_cpu", True)
        except Exception as exc:
            logger.debug("Failed reading config for stt_force_cpu: %s", exc)

        if stt_force_cpu:
            logger.info("STT engine locked to CPU via config (voice.stt_force_cpu=true)")
            return "cpu", "int8", 8

        try:
            import pynvml
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            free_mb = info.free / (1024 * 1024)
            pynvml.nvmlShutdown()
            if free_mb >= 3800:
                return "cuda", "int8_float16", 8
            logger.info("Free VRAM (%.1f MB) < 3800 MB threshold, locking STT to CPU", free_mb)
        except Exception as exc:
            logger.debug("pynvml unavailable (%s), falling back to CPU", exc)
        return "cpu", "int8", 8

    def initialize(self):
        if self._initialized and self._model is not None:
            logger.info("STT engine already initialized")
            return True

        stt_force_cpu = False
        try:
            if CONFIG_PATH.exists():
                cfg_data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                stt_force_cpu = cfg_data.get("voice", {}).get("stt_force_cpu", False)
        except Exception as exc:
            logger.warning("Failed reading config for stt_force_cpu: %s", exc, exc_info=True)

        device = self._device
        compute_type = self._compute_type
        cpu_threads = self._cpu_threads

        if device == "auto":
            device, compute_type, cpu_threads = self._select_optimal_device()
            logger.info("Auto-selected STT device=%s compute_type=%s threads=%d", device, compute_type, cpu_threads)
        elif stt_force_cpu and device == "cuda":
            logger.info("Overriding STT device to CPU due to voice.stt_force_cpu=true")
            device = "cpu"
            compute_type = "int8"
            cpu_threads = 8

        if device == "cuda" and compute_type == "int8":
            compute_type = "float16"
            logger.info("Switched STT compute_type to float16 for CUDA")

        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        t0 = time.perf_counter()
        try:
            self._model = WhisperModel(
                self._model_size,
                device=device,
                compute_type=compute_type,
                cpu_threads=self._cpu_threads,
                download_root=str(PROJECT_ROOT / "Data" / "Models" / "whisper"),
            )
            self._initialized = True
            init_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            logger.info(
                "STT engine initialized in %.1fms | model=%s device=%s compute=%s threads=%d",
                init_ms, self._model_size, device, compute_type, cpu_threads,
            )
            return True
        except Exception as exc:
            logger.exception("Failed to initialize STT engine: %s", exc)
            self._model = None
            return False

    def transcribe_pcm(self, pcm_bytes, sample_rate=16000, language=None) -> Dict[str, Any]:
        if not self._initialized:
            if not self.initialize():
                raise RuntimeError("STT engine not initialized")
        lang = self._detect_language(language)
        audio_in_s = round(len(pcm_bytes) / float(max(sample_rate, 1) * 2), 2)
        logger.debug("Transcribing PCM (%d bytes, %.2fs @ %d Hz, lang=%s)", len(pcm_bytes), audio_in_s, sample_rate, lang)

        pcm_array = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        t0 = time.perf_counter()
        segments, info = self._model.transcribe(
            pcm_array,
            language=lang if lang != "en" else None,
            task="transcribe",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500, "threshold": 0.5},
        )

        # Consume lazy generator BEFORE stopping timer to capture true Whisper inference latency
        text_parts = []
        segments_list = []
        for seg in segments:
            seg_text = seg.text.strip()
            text_parts.append(seg_text)
            segments_list.append({
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg_text,
                "confidence": round(seg.avg_logprob, 4),
            })

        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        duration_s = round(float(getattr(info, "duration", 0.0) or audio_in_s), 3)
        rtf = round((latency_ms / 1000.0) / max(duration_s, 1e-6), 3)
        full_text = " ".join(text_parts).strip()

        result = {
            "text": full_text,
            "language": info.language,
            "language_probability": round(info.language_probability, 4),
            "segments": segments_list,
            "duration": duration_s,
            "latency_ms": latency_ms,
            "rtf": rtf,
        }
        logger.info(
            "STT OK | lang=%s(%.2f) | audio=%.2fs stt=%.1fms (RTF=%.2f) | segs=%d chars=%d | text=%s",
            result["language"],
            result["language_probability"],
            duration_s,
            latency_ms,
            rtf,
            len(segments_list),
            len(full_text),
            full_text[:100] if full_text else "<empty>",
        )
        return result

    def shutdown(self):
        if self._model is not None:
            del self._model
            self._model = None
            self._initialized = False
            logger.info("STT engine shut down")

    def close(self) -> None:
        """Release Whisper resources (delegates to shutdown)."""
        self.shutdown()


if __name__ == "__main__":
    print("=" * 60)
    print("JARVIS STT Engine - Self Test (Stage 3.1)")
    print("=" * 60)
    engine = STTEngine(model_size="small", device="cpu", compute_type="int8", cpu_threads=8)
    ok = engine.initialize()
    print(f"Initialization: {'OK' if ok else 'FAILED'}")
    silent_pcm = np.zeros(16000 * 2, dtype=np.int16).tobytes()
    try:
        result = engine.transcribe_pcm(silent_pcm, sample_rate=16000, language="en")
        print(f"Transcription (silence): '{result['text']}' | lang={result['language']} | stt_ms={result['latency_ms']}")
    except Exception as exc:
        print(f"Transcription error: {exc}")
    engine.shutdown()
    print("=" * 60)
    print("Self-test complete.")
    print("=" * 60)