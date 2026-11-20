# C.E.L.E.S.T.E. Voice - Dual-Engine TTS (Gemini + Gooya Bozorg v1.5)
# Unified Observability: Non-blocking stdout/stderr worker bridge, turn_id correlation,
# explicit fallback diagnostics, and per-stage latency profiling.
from __future__ import annotations

import base64
import gc
import io
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Core.Logger.logger import (
    get_logger,
    get_turn_id,
    pump_subprocess_stream,
    setup_unified_logging,
    turn_context,
)

setup_unified_logging()
logger = get_logger('tts_engine')

CONFIG_PATH = PROJECT_ROOT / 'Configuration' / 'config.json'
GOOYA_WORKER_SCRIPT = PROJECT_ROOT / 'Voice' / 'TTS' / 'gooya_worker.py'
GOOYA_PYTHON_EXE = PROJECT_ROOT / 'Data' / 'Models' / 'gooya' / 'venv' / 'Scripts' / 'python.exe'

_SINGLETON_LOCK = threading.Lock()
_SINGLETON_INSTANCE: Optional['TTSEngine'] = None


class TTSEngine:
    """C.E.L.E.S.T.E. Dual-Engine Speech Synthesis (Gemini REST + Gooya Bozorg v1.5)."""

    DEFAULT_SAMPLE_RATE = 24000
    SPEAK_QUEUE_MAX = 15
    RESP_PREFIX = 'GOOYA_WORKER_RESP:'

    @classmethod
    def get_instance(cls) -> 'TTSEngine':
        global _SINGLETON_INSTANCE
        with _SINGLETON_LOCK:
            if _SINGLETON_INSTANCE is None:
                _SINGLETON_INSTANCE = cls()
            return _SINGLETON_INSTANCE

    def __init__(self, model_name: Optional[str] = None, *args, **kwargs):
        global _SINGLETON_INSTANCE
        self._lock = threading.Lock()
        self._play_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._preload_lock = threading.Lock()
        self._speaking = False
        self._speech_generation_id = 0
        self._queue = []
        self._dsp = None
        self._gooya_proc: Optional[subprocess.Popen] = None
        self._gooya_resp_queue: queue.Queue = queue.Queue()
        self._gooya_loaded: bool = False
        self._preloading: bool = False
        self._last_synth_meta: Dict[str, Any] = {}
        self._config = self._load_config()
        self._engine_mode = self._config.get('engine_mode', 'gemini').lower()
        if self._engine_mode not in ('gemini', 'gooya'):
            self._engine_mode = 'gemini'
        self._initialized = True
        with _SINGLETON_LOCK:
            if _SINGLETON_INSTANCE is None:
                _SINGLETON_INSTANCE = self
        logger.info('TTSEngine initialized with mode: %s', self._engine_mode)

    def initialize(self) -> bool:
        with self._lock:
            self._config = self._load_config()
            self._engine_mode = self._config.get('engine_mode', 'gemini').lower()
            if self._engine_mode not in ('gemini', 'gooya'):
                self._engine_mode = 'gemini'
            self._initialized = True
            should_preload = (
                self._engine_mode == 'gooya'
                and not bool(self._config.get('gooya_auto_unload', False))
                and not self._gooya_loaded
            )
        if should_preload:
            self.preload_gooya(blocking=False)
        return True

    def _load_config(self) -> Dict[str, Any]:
        cfg = {}
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
                cfg = data.get('voice', {}) or {}
                if not cfg.get('online_api_key'):
                    cfg['online_api_key'] = data.get('cloud_ai', {}).get('gemini_api_key', '')
        except Exception as exc:
            logger.warning('Failed to load voice config: %s', exc, exc_info=True)
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
                'gooya_loaded': self._gooya_loaded and worker_alive,
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
            auto_unload = bool(self._config.get('gooya_auto_unload', False))
            already_loaded = self._gooya_loaded and self._gooya_proc is not None and self._gooya_proc.poll() is None

        if old_mode == 'gooya' and new_mode == 'gemini':
            self.unload_gooya()
        elif new_mode == 'gooya' and not auto_unload and not already_loaded:
            logger.info('Switched TTS mode to gooya -> triggering VRAM preload...')
            self.preload_gooya(blocking=False)

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
            auto_unload = bool(self._config.get('gooya_auto_unload', False))
            already_loaded = self._gooya_loaded and self._gooya_proc is not None and self._gooya_proc.poll() is None

        if old_mode == 'gooya' and new_mode == 'gemini':
            self.unload_gooya()
        elif new_mode == 'gooya' and not auto_unload and not already_loaded:
            logger.info('TTSEngine config set to gooya -> preloading Gooya into VRAM...')
            self.preload_gooya(blocking=False)

        logger.info('TTSEngine config updated. Current mode: %s', self._engine_mode)

    def is_ready(self) -> bool:
        return self._initialized

    def synthesize_and_play(self, text: str, blocking: bool = False, turn_id: Optional[str] = None) -> bool:
        return self.speak(text, blocking=blocking, turn_id=turn_id)

    def force_sync_memory_state(self, engine_mode: str, local_engine: Optional[str] = None) -> None:
        self.set_engine_mode(engine_mode)

    @staticmethod
    def clean_text_for_tts(raw_text: str) -> Tuple[str, str]:
        if not raw_text:
            return 'طبیعی', ''
        text = re.sub(r'<(?:think|thought|\|thought\|)>.*?</(?:think|thought|\|thought\|)>', '', raw_text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'^.*?</(?:think|thought|\|thought\|)>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<(?:think|thought|\|thought\|)>.*$', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<action>.*?</action>', '', text, flags=re.DOTALL)
        text = re.sub(r'<fact>.*?</fact>', '', text, flags=re.DOTALL)
        text = re.sub(r'\[Action.*?\]', '', text)
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
        t0 = time.perf_counter()
        api_key = cfg.get('online_api_key') or self._config.get('online_api_key', '')
        if not api_key:
            self._last_synth_meta['gemini_error'] = 'missing_api_key'
            logger.warning('Gemini TTS skipped: No API key configured')
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
            synth_ms = round((time.perf_counter() - t0) * 1000.0, 1)
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
                        audio_s = round(len(pcm) / float(sr), 2) if sr > 0 else 0.0
                        t_dsp = time.perf_counter()
                        pcm = self._apply_dsp(pcm, sr)
                        dsp_ms = round((time.perf_counter() - t_dsp) * 1000.0, 1)
                        self._last_synth_meta.update({
                            'engine_used': 'gemini',
                            'synth_ms': synth_ms,
                            'dsp_ms': dsp_ms,
                            'audio_duration_s': audio_s,
                        })
                        logger.info(
                            'Gemini TTS OK | model=%s voice=%s | audio=%.2fs | synth=%.1fms dsp=%.1fms',
                            model_name, voice_name, audio_s, synth_ms, dsp_ms,
                        )
                        return pcm, sr
                self._last_synth_meta['gemini_error'] = 'no_audio_part_in_response'
                logger.warning('Gemini TTS HTTP 200 but no inline audio part returned (%.1fms)', synth_ms)
            else:
                snippet = resp.text[:240].replace('\n', ' ')
                self._last_synth_meta['gemini_error'] = f'http_{resp.status_code}'
                logger.warning('Gemini TTS HTTP %d (%.1fms): %s', resp.status_code, synth_ms, snippet)
        except Exception as exc:
            self._last_synth_meta['gemini_error'] = f'{type(exc).__name__}: {exc}'
            logger.warning('Gemini TTS request failed: %s', exc, exc_info=True)

        return None, self.DEFAULT_SAMPLE_RATE

    # ------------------------------------------------------------------
    # Engine B: Gooya Bozorg v1.5 Local Engine (Non-Blocking Subprocess Bridge)
    # ------------------------------------------------------------------
    def _drain_worker_stdout(self, proc: subprocess.Popen, resp_queue: queue.Queue) -> None:
        """Continuously pump worker stdout so protocol responses are non-blocking and stray prints are logged."""
        stdout_logger = logging.getLogger('gooya_worker.stdout')
        try:
            if proc.stdout is None:
                return
            for raw_line in iter(proc.stdout.readline, ''):
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith(self.RESP_PREFIX):
                    payload = line[len(self.RESP_PREFIX):]
                    try:
                        resp_queue.put(json.loads(payload))
                    except Exception as parse_exc:
                        logger.error('Failed to parse Gooya worker JSON response: %s | raw=%s', parse_exc, payload[:200])
                        resp_queue.put({'status': 'error', 'error': f'Malformed worker JSON: {parse_exc}'})
                else:
                    stdout_logger.debug('%s', line)
        except Exception as exc:
            logger.debug('Gooya stdout reader exited: %s', exc)
        finally:
            resp_queue.put({'_eof': True, 'returncode': proc.poll()})

    def _ensure_gooya_worker(self) -> subprocess.Popen:
        with self._worker_lock:
            if self._gooya_proc is not None and self._gooya_proc.poll() is None:
                return self._gooya_proc

            self._gooya_loaded = False
            if not GOOYA_PYTHON_EXE.exists():
                raise FileNotFoundError(f'Gooya Python interpreter not found: {GOOYA_PYTHON_EXE}')
            if not GOOYA_WORKER_SCRIPT.exists():
                raise FileNotFoundError(f'Gooya worker script not found: {GOOYA_WORKER_SCRIPT}')

            logger.info('Spawning Gooya worker process: %s', GOOYA_WORKER_SCRIPT)
            resp_q: queue.Queue = queue.Queue()
            proc = subprocess.Popen(
                [str(GOOYA_PYTHON_EXE), '-u', str(GOOYA_WORKER_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            self._gooya_proc = proc
            self._gooya_resp_queue = resp_q

            # Start BOTH stderr structured log pumper and non-blocking stdout reader immediately
            pump_subprocess_stream(
                proc.stderr,
                module_name='gooya_worker',
                default_level=logging.INFO,
                json_prefix='CELESTE_LOG:',
                thread_name='GooyaStderrPump',
            )
            threading.Thread(
                target=self._drain_worker_stdout,
                args=(proc, resp_q),
                daemon=True,
                name='GooyaStdoutPump',
            ).start()

            ready = False
            deadline = time.perf_counter() + 30.0
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                try:
                    msg = resp_q.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue
                if msg.get('_eof'):
                    break
                if msg.get('status') == 'ready':
                    ready = True
                    break

            if not ready:
                rc = proc.poll()
                self._gooya_proc = None
                try:
                    proc.terminate()
                except Exception:
                    pass
                raise RuntimeError(f'Gooya worker failed to start within 30s timeout (returncode={rc})')

            logger.info('Gooya worker process is ready and listening (PID=%d)', proc.pid)
            return proc

    def _read_worker_response(self, proc: subprocess.Popen, timeout: float = 60.0) -> dict:
        deadline = time.perf_counter() + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError(f'Timed out waiting for Gooya worker response ({timeout:.1f}s)')
            try:
                msg = self._gooya_resp_queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if proc.poll() is not None:
                    self._gooya_loaded = False
                    raise RuntimeError(f'Gooya worker terminated unexpectedly with code {proc.returncode}')
                continue
            if msg.get('_eof'):
                self._gooya_loaded = False
                raise RuntimeError(f'Gooya worker closed stdout unexpectedly (returncode={msg.get("returncode")})')
            return msg

    def preload_gooya(self, blocking: bool = False) -> bool:
        """Spawn Gooya worker and preload model weights into RTX 3080 VRAM."""
        caller_turn_id = get_turn_id()

        def _do_load() -> bool:
            with turn_context(caller_turn_id if caller_turn_id != '-' else None, prefix='PRE'):
                with self._preload_lock:
                    with self._lock:
                        if self._engine_mode != 'gooya':
                            return False
                        cfg = dict(self._config)
                        if self._gooya_loaded and self._gooya_proc is not None and self._gooya_proc.poll() is None:
                            return True
                        self._preloading = True
                    try:
                        proc = self._ensure_gooya_worker()
                        model_dir = cfg.get('gooya_model_dir') or str(PROJECT_ROOT / 'Data' / 'Models' / 'gooya')
                        if not Path(model_dir).is_absolute():
                            model_dir = str(PROJECT_ROOT / model_dir)
                        precision = cfg.get('gooya_precision', 'bf16')

                        req = {
                            'command': 'load',
                            'model_dir': model_dir,
                            'precision': precision,
                            'turn_id': get_turn_id(),
                        }
                        logger.info('Preloading Gooya Bozorg v1.5 into VRAM (precision=%s)...', precision)
                        with self._worker_lock:
                            proc.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
                            proc.stdin.flush()
                            resp = self._read_worker_response(proc, timeout=120.0)

                        if resp.get('status') == 'ok':
                            with self._lock:
                                self._gooya_loaded = True
                            logger.info(
                                'Gooya preload complete in %ss | VRAM alloc=%s MB, reserved=%s MB',
                                resp.get('load_time_s', '?'),
                                resp.get('vram_allocated_mb', '?'),
                                resp.get('vram_reserved_mb', '?'),
                            )
                            return True
                        logger.error('Gooya preload error: %s', resp.get('error'))
                        return False
                    except Exception as exc:
                        logger.exception('Gooya preload failed: %s', exc)
                        return False
                    finally:
                        with self._lock:
                            self._preloading = False

        if blocking:
            return _do_load()
        threading.Thread(target=_do_load, daemon=True, name='GooyaPreloadThread').start()
        return True

    def _synthesize_gooya(self, text: str, cfg: dict) -> Tuple[Optional[np.ndarray], int]:
        t0 = time.perf_counter()
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
                'turn_id': get_turn_id(),
            }

            with self._worker_lock:
                proc.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
                proc.stdin.flush()
                resp = self._read_worker_response(proc, timeout=90.0)

            synth_ms = round((time.perf_counter() - t0) * 1000.0, 1)

            if resp.get('status') == 'ok' and resp.get('audio_b64'):
                with self._lock:
                    self._gooya_loaded = True
                raw_bytes = base64.b64decode(resp['audio_b64'])
                pcm = np.frombuffer(raw_bytes, dtype=np.float32)
                sr = int(resp.get('sample_rate', self.DEFAULT_SAMPLE_RATE))
                t_dsp = time.perf_counter()
                pcm = self._apply_dsp(pcm, sr)
                dsp_ms = round((time.perf_counter() - t_dsp) * 1000.0, 1)

                self._last_synth_meta.update({
                    'engine_used': 'gooya',
                    'synth_ms': synth_ms,
                    'dsp_ms': dsp_ms,
                    'audio_duration_s': resp.get('audio_duration_s', 0.0),
                    'rtf': resp.get('rtf', 0.0),
                })
                logger.info(
                    'Gooya TTS OK | audio=%ss | synth=%.1fms (conds=%sms gen=%sms) dsp=%.1fms RTF=%s | VRAM=%s MB',
                    resp.get('audio_duration_s', '?'),
                    synth_ms,
                    resp.get('conds_ms', '?'),
                    resp.get('gen_ms', '?'),
                    dsp_ms,
                    resp.get('rtf', '?'),
                    resp.get('vram_allocated_mb', '?'),
                )

                if cfg.get('gooya_auto_unload', False):
                    self.unload_gooya()

                return pcm, sr
            else:
                err_msg = str(resp.get('error', 'unknown_worker_error'))
                self._last_synth_meta['gooya_error'] = err_msg
                logger.error('Gooya worker returned error: %s', err_msg)
        except Exception as exc:
            self._last_synth_meta['gooya_error'] = f'{type(exc).__name__}: {exc}'
            logger.exception('Gooya synthesis failed: %s', exc)
            if self._gooya_proc is not None and self._gooya_proc.poll() is not None:
                self._gooya_proc = None
                self._gooya_loaded = False

        return None, self.DEFAULT_SAMPLE_RATE

    def unload_gooya(self) -> None:
        """Dynamic VRAM Management: unloads Gooya worker and completely releases VRAM."""
        with self._worker_lock:
            self._gooya_loaded = False
            if self._gooya_proc is not None:
                try:
                    if self._gooya_proc.poll() is None:
                        self._gooya_proc.stdin.write(json.dumps({'command': 'exit', 'turn_id': get_turn_id()}) + '\n')
                        self._gooya_proc.stdin.flush()
                        self._gooya_proc.wait(timeout=3.0)
                except Exception as exc:
                    logger.warning('Gooya graceful exit timed out/failed (%s); terminating process', exc)
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
            logger.warning('DSP processing failed (bypassing DSP): %s', exc, exc_info=True)
            return pcm

    def _playback_audio(self, pcm: np.ndarray, sample_rate: int = 24000, blocking: bool = True) -> float:
        if pcm is None or len(pcm) == 0:
            return 0.0

        caller_tid = get_turn_id()

        def _play() -> float:
            with turn_context(caller_tid if caller_tid != '-' else None, prefix='PLY'):
                t0 = time.perf_counter()
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
                    logger.exception('Audio playback error: %s', exc)
                return round((time.perf_counter() - t0) * 1000.0, 1)

        if blocking:
            return _play()
        threading.Thread(target=_play, daemon=True, name='TTSPlayback').start()
        return 0.0

    # ------------------------------------------------------------------
    # Public Voice Output Methods
    # ------------------------------------------------------------------
    def synthesize(self, text: str, tone: Optional[str] = None, turn_id: Optional[str] = None) -> Tuple[Optional[np.ndarray], int]:
        eff_tid = turn_id or get_turn_id()
        with turn_context(eff_tid if eff_tid != '-' else None, prefix='TTS'):
            auto_tone, clean_text = self.clean_text_for_tts(text)
            used_tone = tone or auto_tone
            if not clean_text:
                return None, self.DEFAULT_SAMPLE_RATE

            with self._lock:
                cfg = dict(self._config)
                mode = self._engine_mode

            self._last_synth_meta = {
                'engine_requested': mode,
                'engine_used': 'none',
                'fallback_triggered': False,
                'fallback_reason': '',
                'synth_ms': 0.0,
                'dsp_ms': 0.0,
                'audio_duration_s': 0.0,
            }

            if mode == 'gemini':
                pcm, sr = self._synthesize_gemini(clean_text, used_tone, cfg)
                if pcm is not None and len(pcm) > 0:
                    return pcm, sr
                reason = self._last_synth_meta.get('gemini_error', 'gemini_unavailable')
                self._last_synth_meta['fallback_triggered'] = True
                self._last_synth_meta['fallback_reason'] = reason
                logger.warning('TTS FALLBACK triggered: gemini -> gooya (reason=%s)', reason)
                return self._synthesize_gooya(clean_text, cfg)
            else:
                pcm, sr = self._synthesize_gooya(clean_text, cfg)
                if pcm is not None and len(pcm) > 0:
                    return pcm, sr
                reason = self._last_synth_meta.get('gooya_error', 'gooya_unavailable')
                self._last_synth_meta['fallback_triggered'] = True
                self._last_synth_meta['fallback_reason'] = reason
                logger.warning('TTS FALLBACK triggered: gooya -> gemini (reason=%s)', reason)
                return self._synthesize_gemini(clean_text, used_tone, cfg)

    def speak(self, text: str, blocking: bool = False, turn_id: Optional[str] = None) -> bool:
        tone, clean_text = self.clean_text_for_tts(text)
        if not clean_text:
            return False

        eff_tid = turn_id or get_turn_id()
        with self._lock:
            self._speech_generation_id += 1
            gen_id = self._speech_generation_id
            self._queue.append(text)
            if len(self._queue) > self.SPEAK_QUEUE_MAX:
                self._queue.pop(0)

        if blocking:
            return self._speak_worker(clean_text, tone, gen_id, eff_tid)
        else:
            threading.Thread(
                target=self._speak_worker,
                args=(clean_text, tone, gen_id, eff_tid),
                daemon=True,
                name='TTSSpeakWorker',
            ).start()
            return True

    def _speak_worker(self, clean_text: str, tone: str, gen_id: int, turn_id: Optional[str] = None) -> bool:
        with turn_context(turn_id if turn_id and turn_id != '-' else None, prefix='TTS'):
            t_total = time.perf_counter()
            with self._play_lock:
                with self._lock:
                    if gen_id != self._speech_generation_id:
                        logger.debug('Skipped superseded TTS utterance (gen_id=%d)', gen_id)
                        return False
                    self._speaking = True

                try:
                    pcm, sr = self.synthesize(clean_text, tone=tone, turn_id=get_turn_id())
                    meta = dict(self._last_synth_meta)
                    if pcm is not None and len(pcm) > 0 and gen_id == self._speech_generation_id:
                        play_ms = self._playback_audio(pcm, sample_rate=sr, blocking=True)
                        total_ms = round((time.perf_counter() - t_total) * 1000.0, 1)
                        logger.info(
                            'TTS Turn Complete | req=%s used=%s fallback=%s%s | chars=%d audio=%ss | synth=%.1fms dsp=%.1fms play=%.1fms total=%.1fms',
                            meta.get('engine_requested', '?'),
                            meta.get('engine_used', '?'),
                            meta.get('fallback_triggered', False),
                            f" ({meta.get('fallback_reason')})" if meta.get('fallback_triggered') else '',
                            len(clean_text),
                            meta.get('audio_duration_s', '?'),
                            float(meta.get('synth_ms', 0.0)),
                            float(meta.get('dsp_ms', 0.0)),
                            play_ms,
                            total_ms,
                        )
                        return True
                    logger.error(
                        'TTS Turn Failed (no audio produced) | req=%s fallback=%s reason=%s | chars=%d',
                        meta.get('engine_requested', '?'),
                        meta.get('fallback_triggered', False),
                        meta.get('fallback_reason') or meta.get('gemini_error') or meta.get('gooya_error') or 'unknown',
                        len(clean_text),
                    )
                    return False
                except Exception as exc:
                    logger.exception('TTS speak worker error: %s', exc)
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


def get_tts_engine() -> TTSEngine:
    return TTSEngine.get_instance()


if __name__ == '__main__':
    print('=' * 60)
    print('C.E.L.E.S.T.E. TTSEngine Self-Test (Dual-Engine: Gemini + Gooya)')
    print('=' * 60)

    t_start = time.time()
    engine = TTSEngine()
    engine.preload_gooya = lambda blocking=False: True  # mock GPU spawn for fast unit test
    status = engine.get_status()
    print(f'Initialization: OK | Engine Mode: {status["engine_mode"]}')
    assert status['initialized'] is True

    test_raw = 'سلام [لحن: شاد و پرانرژی] من C.E.L.E.S.T.E. هستم! <think>thought</think><think>unclosed'
    tone, clean = engine.clean_text_for_tts(test_raw)
    print(f'Clean text: "{clean}" | Tone: "{tone}"')
    assert 'سلست' in clean and '<think>' not in clean and 'unclosed' not in clean
    assert tone == 'شاد و پرانرژی'

    engine.set_engine_mode('gooya')
    assert engine.get_status()['engine_mode'] == 'gooya'

    engine.update_config({'engine_mode': 'gemini', 'gemini_voice_name': 'Kore'})
    st_after = engine.get_status()
    assert st_after['engine_mode'] == 'gemini'
    assert st_after['gemini_voice_name'] == 'Kore'
    assert st_after['gooya_worker_alive'] is False

    original_gemini = engine._synthesize_gemini
    engine._synthesize_gemini = lambda text, tone, cfg: (
        engine._apply_dsp(np.zeros(24000, dtype=np.float32), 24000), 24000
    )
    pcm, sr = engine.synthesize('سلست آماده است.')
    assert pcm is not None and len(pcm) == 24000 and sr == 24000
    engine._synthesize_gemini = original_gemini

    engine.shutdown()
    print(f'All self-tests passed in {time.time() - t_start:.2f} seconds.')
    print('=' * 60)