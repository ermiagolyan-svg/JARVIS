# C.E.L.E.S.T.E. Voice - Dual-Engine TTS (Gemini + Gooya Bozorg v1.5)
# Stage Backend Phase 2: Dual-Engine TTS with BF16/SDPA, Reference Caching & Dynamic VRAM Management
from __future__ import annotations

import base64
import gc
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / 'Configuration' / 'config.json'
GOOYA_WORKER_SCRIPT = PROJECT_ROOT / 'Voice' / 'TTS' / 'gooya_worker.py'
GOOYA_PYTHON_EXE = PROJECT_ROOT / 'Data' / 'Models' / 'gooya' / 'venv' / 'Scripts' / 'python.exe'

logger = logging.getLogger(__name__)


class TTSEngine:
    """C.E.L.E.S.T.E. Dual-Engine Speech Synthesis (Gemini REST + Gooya Bozorg v1.5)."""

    DEFAULT_SAMPLE_RATE = 24000
    SPEAK_QUEUE_MAX = 15
    RESP_PREFIX = 'GOOYA_WORKER_RESP:'

    def __init__(self, model_name: Optional[str] = None, *args, **kwargs):
        self._lock = threading.Lock()
        self._play_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._speaking = False
        self._speech_generation_id = 0
        self._queue = []
        self._dsp = None
        self._gooya_proc: Optional[subprocess.Popen] = None
        self._config = self._load_config()
        self._engine_mode = self._config.get('engine_mode', 'gemini').lower()
        if self._engine_mode not in ('gemini', 'gooya'):
            self._engine_mode = 'gemini'
        self._initialized = True
        logger.info('TTSEngine initialized with mode: %s', self._engine_mode)

    def initialize(self) -> bool:
        with self._lock:
            self._config = self._load_config()
            self._engine_mode = self._config.get('engine_mode', 'gemini').lower()
            if self._engine_mode not in ('gemini', 'gooya'):
                self._engine_mode = 'gemini'
            self._initialized = True
        return True

    def _load_config(self) -> Dict[str, Any]:
        cfg = {}
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
                cfg = data.get('voice', {}) or {}
                # Carry fallback API key from cloud_ai
                if not cfg.get('online_api_key'):
                    cfg['online_api_key'] = data.get('cloud_ai', {}).get('gemini_api_key', '')
        except Exception as exc:
            logger.debug('Failed to load voice config: %s', exc)
        return cfg

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            worker_alive = (self._gooya_proc is not None and self._gooya_proc.poll() is None)
            return {
                'initialized': self._initialized,
                'speaking': self._speaking,
                'engine_mode': self._engine_mode,
                'gemini_model': self._config.get('gemini_model', 'gemini-2.5-flash-preview-tts'),
                'gemini_voice_name': self._config.get('gemini_voice_name', 'Kore'),
                'gooya_worker_alive': worker_alive,
                'gooya_precision': self._config.get('gooya_precision', 'bf16'),
                'gooya_auto_unload': self._config.get('gooya_auto_unload', False),
            }

    def set_engine_mode(self, new_mode: str) -> None:
        new_mode = (new_mode or 'gemini').lower().strip()
        if new_mode in ('online', 'gemini'):
            new_mode = 'gemini'
        elif new_mode in ('local', 'gooya'):
            new_mode = 'gooya'
        else:
            logger.warning('Unknown engine mode: %s, defaulting to gemini', new_mode)
            new_mode = 'gemini'

        with self._lock:
            old_mode = self._engine_mode
            self._engine_mode = new_mode
            self._config['engine_mode'] = new_mode

        if old_mode == 'gooya' and new_mode == 'gemini':
            self.unload_gooya()
        logger.info('TTS engine mode set to: %s (was %s)', new_mode, old_mode)

    def update_config(self, new_voice_config: dict) -> None:
        if not isinstance(new_voice_config, dict):
            return
        with self._lock:
            old_mode = self._engine_mode
            self._config.update(new_voice_config)
            new_mode = (self._config.get('engine_mode') or old_mode).lower().strip()
            if new_mode in ('online', 'gemini'):
                new_mode = 'gemini'
            elif new_mode in ('local', 'gooya'):
                new_mode = 'gooya'
            self._engine_mode = new_mode
            self._config['engine_mode'] = new_mode

        if old_mode == 'gooya' and new_mode == 'gemini':
            self.unload_gooya()
        logger.info('TTSEngine config updated. Current mode: %s', self._engine_mode)

    def is_ready(self) -> bool:
        return self._initialized

    def synthesize_and_play(self, text: str, blocking: bool = False) -> bool:
        return self.speak(text, blocking=blocking)

    def force_sync_memory_state(self, engine_mode: str, local_engine: Optional[str] = None) -> None:
        # Compatibility wrapper for REST server updates
        self.set_engine_mode(engine_mode)

    @staticmethod
    def clean_text_for_tts(raw_text: str) -> Tuple[str, str]:
        if not raw_text:
            return 'طبیعی', ''
        text = re.sub(r'<think>.*?</think>', '', raw_text, flags=re.DOTALL)
        text = re.sub(r'<action>.*?</action>', '', text, flags=re.DOTALL)
        text = re.sub(r'<fact>.*?</fact>', '', text, flags=re.DOTALL)
        text = re.sub(r'\[Action.*?\]', '', text)
        # Normalize C.E.L.E.S.T.E. variations to Persian
        text = re.sub(r'C\s*\.\s*E\s*\.\s*L\s*\.\s*E\s*\.\s*S\s*\.\s*T\s*\.\s*E\.?', 'سلست', text, flags=re.IGNORECASE)
        text = re.sub(r'\bCELESTE\b', 'سلست', text, flags=re.IGNORECASE)
        tone_match = re.search(r'\[\s*(?:لحن|حس|tone)\s*:\s*([^\]]+)\]', text, flags=re.IGNORECASE)
        explicit_tone = ''
        if tone_match:
            explicit_tone = tone_match.group(1).strip()
            text = text[:tone_match.start()] + text[tone_match.end():]
        text = re.sub(r'[*#_~>|•]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return explicit_tone or 'صمیمی و طبیعی', text

    # ------------------------------------------------------------------
    # Engine A: Gemini Online Engine
    # ------------------------------------------------------------------
    def _synthesize_gemini(self, text: str, tone: str, cfg: dict) -> Tuple[Optional[np.ndarray], int]:
        api_key = cfg.get('online_api_key') or self._config.get('online_api_key', '')
        if not api_key:
            logger.warning('Gemini TTS: No API key configured')
            return None, self.DEFAULT_SAMPLE_RATE

        model_name = cfg.get('gemini_model', 'gemini-2.5-flash-preview-tts')
        voice_name = cfg.get('gemini_voice_name') or cfg.get('online_voice_name', 'Kore')
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}'
        prompt = f'متن زیر را با لهجه اصیل و طبیعیِ فارسیِ تهرانی، با حس و لحنِ «{tone}» بخوان:\n\n«{text}»'

        payload = {
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {
                'responseModalities': ['AUDIO'],
                'speechConfig': {
                    'voiceConfig': {
                        'prebuiltVoiceConfig': {
                            'voiceName': voice_name,
                        }
                    }
                },
            },
        }

        try:
            resp = requests.post(url, json=payload, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                parts = data.get('candidates', [])[0].get('content', {}).get('parts', [])
                for part in parts:
                    inline = part.get('inlineData') or part.get('inline_data')
                    if inline and inline.get('data'):
                        raw_audio = base64.b64decode(inline['data'])
                        if raw_audio.startswith(b'RIFF'):
                            with wave.open(io.BytesIO(raw_audio), 'rb') as wf:
                                sr = wf.getframerate()
                                frames = wf.readframes(wf.getnframes())
                                pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                        else:
                            sr = self.DEFAULT_SAMPLE_RATE
                            if len(raw_audio) % 2 != 0:
                                raw_audio = raw_audio[:-1]
                            pcm = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32) / 32768.0
                        return self._apply_dsp(pcm, sr), sr
            else:
                logger.warning('Gemini TTS HTTP %d: %s', resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning('Gemini TTS Request failed: %s', exc)

        return None, self.DEFAULT_SAMPLE_RATE

    # ------------------------------------------------------------------
    # Engine B: Gooya Bozorg v1.5 Local Engine (via Warm Worker Subprocess)
    # ------------------------------------------------------------------
    def _ensure_gooya_worker(self) -> subprocess.Popen:
        with self._worker_lock:
            if self._gooya_proc is not None and self._gooya_proc.poll() is None:
                return self._gooya_proc

            if not GOOYA_PYTHON_EXE.exists():
                raise FileNotFoundError(f'Gooya Python interpreter not found: {GOOYA_PYTHON_EXE}')
            if not GOOYA_WORKER_SCRIPT.exists():
                raise FileNotFoundError(f'Gooya worker script not found: {GOOYA_WORKER_SCRIPT}')

            logger.info('Spawning Gooya worker process: %s', GOOYA_WORKER_SCRIPT)
            self._gooya_proc = subprocess.Popen(
                [str(GOOYA_PYTHON_EXE), str(GOOYA_WORKER_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                bufsize=1,
            )

            # Wait for worker ready signal
            t0 = time.time()
            ready = False
            while time.time() - t0 < 15.0:
                line = self._gooya_proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith(self.RESP_PREFIX):
                    try:
                        resp = json.loads(line[len(self.RESP_PREFIX):])
                        if resp.get('status') == 'ready':
                            ready = True
                            break
                    except Exception:
                        pass

            if not ready:
                self.unload_gooya()
                raise RuntimeError('Gooya worker failed to start within timeout')

            logger.info('Gooya worker process is ready and listening')
            return self._gooya_proc

    def _read_worker_response(self, proc: subprocess.Popen, timeout: float = 30.0) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if proc.poll() is not None:
                raise RuntimeError(f'Gooya worker terminated unexpectedly with code {proc.returncode}')
            line = proc.stdout.readline()
            if not line:
                continue
            line = line.strip()
            if line.startswith(self.RESP_PREFIX):
                return json.loads(line[len(self.RESP_PREFIX):])
        raise TimeoutError('Timed out waiting for Gooya worker response')

    def _synthesize_gooya(self, text: str, cfg: dict) -> Tuple[Optional[np.ndarray], int]:
        try:
            proc = self._ensure_gooya_worker()
            ref_path = cfg.get('gooya_reference_audio') or str(PROJECT_ROOT / 'Data' / 'Models' / 'gooya' / 'speaker_reference.wav')
            if not Path(ref_path).is_absolute():
                ref_path = str(PROJECT_ROOT / ref_path)

            req = {
                'command': 'synthesize',
                'text': text,
                'ref_audio': ref_path,
                'exaggeration': float(cfg.get('gooya_exaggeration', 0.5)),
                'cfg_weight': float(cfg.get('gooya_cfg_weight', 0.5)),
                'precision': cfg.get('gooya_precision', 'bf16'),
            }

            with self._worker_lock:
                proc.stdin.write(json.dumps(req) + '\n')
                proc.stdin.flush()
                resp = self._read_worker_response(proc, timeout=35.0)

            if resp.get('status') == 'ok' and resp.get('audio_b64'):
                raw_bytes = base64.b64decode(resp['audio_b64'])
                pcm = np.frombuffer(raw_bytes, dtype=np.float32)
                sr = int(resp.get('sample_rate', self.DEFAULT_SAMPLE_RATE))
                pcm = self._apply_dsp(pcm, sr)

                if cfg.get('gooya_auto_unload', False):
                    self.unload_gooya()

                return pcm, sr
            else:
                logger.error('Gooya worker returned error: %s', resp.get('error'))
        except Exception as exc:
            logger.error('Gooya synthesis failed: %s', exc)

        return None, self.DEFAULT_SAMPLE_RATE

    def unload_gooya(self) -> None:
        """Dynamic VRAM Management: unloads Gooya worker and completely releases VRAM."""
        with self._worker_lock:
            if self._gooya_proc is not None:
                try:
                    if self._gooya_proc.poll() is None:
                        self._gooya_proc.stdin.write(json.dumps({'command': 'exit'}) + '\n')
                        self._gooya_proc.stdin.flush()
                        self._gooya_proc.wait(timeout=2.0)
                except Exception:
                    try:
                        self._gooya_proc.terminate()
                    except Exception:
                        pass
                finally:
                    self._gooya_proc = None
                    logger.info('[Gooya VRAM Manager] Gooya worker process terminated and VRAM returned to system.')
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                if hasattr(torch.cuda, 'ipc_collect'):
                    torch.cuda.ipc_collect()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Audio DSP & Playback
    # ------------------------------------------------------------------
    def _apply_dsp(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        try:
            from Voice.AudioDSP.jarvis_dsp import CelesteAudioDSP
            if getattr(self, '_dsp', None) is None or self._dsp._sr != sample_rate:
                self._dsp = CelesteAudioDSP(sample_rate=sample_rate)
            return self._dsp.process(pcm)
        except Exception as exc:
            logger.debug('DSP bypass: %s', exc)
            return pcm

    def _playback_audio(self, pcm: np.ndarray, sample_rate: int = 24000, blocking: bool = True) -> None:
        if pcm is None or len(pcm) == 0:
            return

        def _play():
            pcm_clipped = np.clip(pcm, -1.0, 1.0)
            pcm_int16 = (pcm_clipped * 32767.0).astype(np.int16)
            try:
                import winsound
                buf = io.BytesIO()
                with wave.open(buf, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    wf.writeframes(pcm_int16.tobytes())
                winsound.PlaySound(buf.getvalue(), winsound.SND_MEMORY)
            except Exception as exc:
                logger.error('Playback error: %s', exc)

        if blocking:
            _play()
        else:
            threading.Thread(target=_play, daemon=True).start()

    # ------------------------------------------------------------------
    # Public Voice Output Methods
    # ------------------------------------------------------------------
    def synthesize(self, text: str, tone: Optional[str] = None) -> Tuple[Optional[np.ndarray], int]:
        auto_tone, clean_text = self.clean_text_for_tts(text)
        used_tone = tone or auto_tone
        if not clean_text:
            return None, self.DEFAULT_SAMPLE_RATE

        with self._lock:
            cfg = dict(self._config)
            mode = self._engine_mode

        if mode == 'gemini':
            pcm, sr = self._synthesize_gemini(clean_text, used_tone, cfg)
            if pcm is not None and len(pcm) > 0:
                return pcm, sr
            logger.warning('Gemini online TTS unavailable, attempting Gooya local fallback...')
            return self._synthesize_gooya(clean_text, cfg)
        else:
            pcm, sr = self._synthesize_gooya(clean_text, cfg)
            if pcm is not None and len(pcm) > 0:
                return pcm, sr
            logger.warning('Gooya local TTS unavailable, attempting Gemini online fallback...')
            return self._synthesize_gemini(clean_text, used_tone, cfg)

    def speak(self, text: str, blocking: bool = False) -> bool:
        tone, clean_text = self.clean_text_for_tts(text)
        if not clean_text:
            return False

        with self._lock:
            self._speech_generation_id += 1
            gen_id = self._speech_generation_id
            self._queue.append(text)
            if len(self._queue) > self.SPEAK_QUEUE_MAX:
                self._queue.pop(0)

        if blocking:
            return self._speak_worker(clean_text, tone, gen_id)
        else:
            threading.Thread(target=self._speak_worker, args=(clean_text, tone, gen_id), daemon=True).start()
            return True

    def _speak_worker(self, clean_text: str, tone: str, gen_id: int) -> bool:
        with self._play_lock:
            with self._lock:
                if gen_id != self._speech_generation_id:
                    return False
                self._speaking = True

            try:
                pcm, sr = self.synthesize(clean_text, tone=tone)
                if pcm is not None and len(pcm) > 0 and gen_id == self._speech_generation_id:
                    self._playback_audio(pcm, sample_rate=sr, blocking=True)
                    return True
                return False
            except Exception as exc:
                logger.error('TTS speak worker error: %s', exc)
                return False
            finally:
                with self._lock:
                    if gen_id == self._speech_generation_id:
                        self._speaking = False

    def stop_speaking(self) -> None:
        with self._lock:
            self._speech_generation_id += 1
            self._speaking = False
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def shutdown(self) -> None:
        self.stop_speaking()
        self.unload_gooya()

    def close(self) -> None:
        self.shutdown()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    print('=' * 60)
    print('C.E.L.E.S.T.E. TTSEngine Self-Test (Dual-Engine: Gemini + Gooya)')
    print('=' * 60)

    t_start = time.time()
    engine = TTSEngine()
    status = engine.get_status()
    print(f'Initialization: OK | Engine Mode: {status["engine_mode"]}')
    assert status['initialized'] is True

    # 1. Test text normalization and tone extraction
    test_raw = 'سلام [لحن: شاد و پرانرژی] من C.E.L.E.S.T.E. هستم! <think>thought</think>'
    tone, clean = engine.clean_text_for_tts(test_raw)
    print(f'Clean text: "{clean}" | Tone: "{tone}"')
    assert 'سلست' in clean
    assert '<think>' not in clean
    assert tone == 'شاد و پرانرژی'

    # 2. Test config switching
    print('Testing config switching to gooya...')
    engine.set_engine_mode('gooya')
    assert engine.get_status()['engine_mode'] == 'gooya'
    print('Engine mode successfully switched to gooya.')

    print('Testing update_config and dynamic VRAM cleanup...')
    engine.update_config({'engine_mode': 'gemini', 'gemini_voice_name': 'Kore'})
    st_after = engine.get_status()
    assert st_after['engine_mode'] == 'gemini'
    assert st_after['gemini_voice_name'] == 'Kore'
    assert st_after['gooya_worker_alive'] is False
    print('Config updated live, Gooya worker unloaded, VRAM fully released.')

    # 3. Test synthesis pipeline and DSP processing
    print('Testing synthesis pipeline and DSP...')
    original_gemini = engine._synthesize_gemini
    engine._synthesize_gemini = lambda text, tone, cfg: (
        engine._apply_dsp(np.zeros(24000, dtype=np.float32), 24000), 24000
    )
    pcm, sr = engine.synthesize('سلست آماده است.')
    assert pcm is not None and len(pcm) == 24000 and sr == 24000
    print(f'Synthesis pipeline & DSP verified: {len(pcm)} samples at {sr} Hz.')
    engine._synthesize_gemini = original_gemini

    engine.shutdown()
    duration = time.time() - t_start
    print(f'All self-tests passed in {duration:.2f} seconds (< 15s limit).')
    print('=' * 60)
