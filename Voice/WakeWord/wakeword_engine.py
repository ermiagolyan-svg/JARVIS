'''JARVIS Voice - Continuous Wake-Word Detection Engine (Stage 3.3).'''
import collections
import logging
import sys
from typing import Callable, Optional

import numpy as np

PROJECT_ROOT = __import__('pathlib').Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK_SAMPLES = int(1280)


class WakeWordEngine:
    '''Continuous wake-word detector using openwakeword + sounddevice.'''

    def __init__(self, on_wake_word_detected: Optional[Callable] = None,
                 speech_recorded_callback: Optional[Callable] = None):
        self._on_wake_word_detected = on_wake_word_detected or (lambda text: None)
        self._speech_recorded_callback = speech_recorded_callback or (lambda pcm: None)
        self._muted = False
        self._audio_source = 'mic'
        self._model = None
        self._ring = collections.deque(maxlen=160000)
        self._recording = []
        self._stream = None
        self._initialized = False

    def initialize(self) -> bool:
        if self._initialized:
            return True
        try:
            from openwakeword import Model as OWModel
            try:
                from openwakeword.utils import download_models
                download_models(model_names=['hey_jarvis'], verbose=False)
            except Exception:
                pass
            # Windows has no tflite-runtime wheel; use onnxruntime directly
            if sys.platform == 'win32':
                logger.info('Windows detected: using onnxruntime model format')
                self._model = OWModel(
                    wakeword_models=['hey_jarvis'], device='cpu', inference_framework='onnx')
            else:
                self._model = OWModel(wakeword_models=['hey_jarvis'], device='cpu')
            self._initialized = True
            logger.info('WakeWordEngine initialized (hey_jarvis, CPU)')
            return True
        except Exception as exc:
            logger.error('Failed to initialize wake-word engine: %s', exc)
            return False

    def start(self):
        if not self._initialized:
            if not self.initialize():
                raise RuntimeError('WakeWordEngine not initialized')
        self._ring.clear()
        if self._audio_source == 'network':
            logger.info('WakeWordEngine: network-source mode (feed_audio only)')
        else:
            import sounddevice as sd
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, blocksize=CHUNK_SAMPLES,
                channels=1, dtype=np.int16, callback=self._on_audio_chunk)
            self._stream.start()
            logger.info('WakeWordEngine: mic-stream started (16kHz int16 mono)')

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        logger.info('WakeWordEngine stopped')

    def set_muted(self, muted: bool):
        self._muted = muted

    def is_muted(self) -> bool:
        return self._muted

    def get_current_rms(self) -> float:
        if not self._ring:
            return 0.0
        arr = np.frombuffer(b''.join(self._ring), dtype=np.int16) / 32768.0
        return float(np.sqrt(np.mean(arr ** 2)))

    def set_audio_source(self, source: str):
        self._audio_source = source if source in ('mic', 'network') else 'mic'

    def feed_audio(self, pcm_bytes: bytes):
        if not self._initialized:
            return
        arr = np.frombuffer(pcm_bytes, dtype=np.int16)
        for i in range(0, len(arr), CHUNK_SAMPLES):
            chunk = arr[i:i + CHUNK_SAMPLES].tobytes()
            self._ring.append(chunk)
            if not self._muted:
                self._check_wake_word(chunk)

    def _on_audio_chunk(self, data, frames, time_info, status):
        if status or self._muted:
            return
        chunk = data.tobytes()
        self._ring.append(chunk)
        self._check_wake_word(chunk)

    def _check_wake_word(self, chunk: bytes):
        arr = np.frombuffer(chunk, dtype=np.int16)
        scores = self._model.predict(x=arr)
        for ww_name, score in scores.items():
            if score >= 0.5:
                logger.info('Wake-word detected: %s (score=%.4f)', ww_name, score)
                self._on_wake_word_detected(ww_name)
                self._capture_response(chunk)
                break

    def _capture_response(self, initial_chunk: bytes):
        self._recording = [initial_chunk]
        silence_frames = 0
        max_frames = SAMPLE_RATE * 15 // CHUNK_SAMPLES
        for i in range(max_frames):
            if not self._ring:
                break
            frame = self._ring[-(i + 1)]
            arr = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(arr ** 2)))
            if rms > 0.005:
                silence_frames = 0
            else:
                silence_frames += 1
            self._recording.append(frame)
            if silence_frames >= int(1.2 * SAMPLE_RATE / CHUNK_SAMPLES):
                break
        pcm_out = b''.join(reversed(self._recording))
        logger.info('Response captured: %d bytes (%.2fs)', len(pcm_out),
                    len(pcm_out) / (SAMPLE_RATE * 2))
        self._speech_recorded_callback(pcm_out)
        self._recording = []


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    print('=' * 60)
    print('JARVIS WakeWordEngine - Self Test (Stage 3.3)')
    print('=' * 60)
    engine = WakeWordEngine(
        on_wake_word_detected=lambda ww: logger.info('WAKE: %s', ww),
        speech_recorded_callback=lambda pcm: logger.info('RECORD: %d bytes', len(pcm)))
    ok = engine.initialize()
    print(f'Initialization: {"OK" if ok else "FAILED"}')
    for _ in range(5):
        engine.feed_audio(np.zeros(CHUNK_SAMPLES, dtype=np.int16).tobytes())
    engine.set_muted(True)
    assert engine.is_muted() is True
    rms = engine.get_current_rms()
    print(f'RMS (muted, silent): {rms:.6f}')
    engine.set_muted(False)
    assert engine.is_muted() is False
    print('set_muted(True/False) + get_current_rms(): OK')
    engine.stop()
    print('=' * 60)
    print('STAGE 3.3 OK')
    print('=' * 60)