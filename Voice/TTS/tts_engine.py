# C.E.L.E.S.T.E. Voice - Dual-Engine TTS (Gemini + Gooya Bozorg v1.5)
# Low-Latency Streaming Architecture:
#   - Persian/English adaptive 2-tier sentence/clause chunker (TTFA ~1.5-2.5s)
#   - Producer-Consumer pipelined Gooya synthesis & playback with instant barge-in (stop_speaking)
#   - Clean Gemini fallback (for chunk 1 or remaining tail), Windows venv process-tree cleanup,
#     offline HF env, startup preload + CUDA warmup, and full turn_id observability
from __future__ import annotations

import atexit
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
from typing import Any, Dict, List, Optional, Tuple

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
    """C.E.L.E.S.T.E. Dual-Engine Speech Synthesis (Gemini REST + Pipelined Gooya Bozorg v1.5)."""

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

    def __init__(self, model_name: Optional[str] = None, auto_preload: bool = True, *args, **kwargs):
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
        self._gooya_real_pid: Optional[int] = None
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

        atexit.register(self._atexit_cleanup)
        logger.info('TTSEngine initialized with mode: %s', self._engine_mode)

        if (
            auto_preload
            and not os.environ.get('CELESTE_NO_AUTO_PRELOAD')
            and self._engine_mode == 'gooya'
            and not bool(self._config.get('gooya_auto_unload', False))
        ):
            self.preload_gooya(blocking=False)

    def _atexit_cleanup(self) -> None:
        try:
            if self._gooya_proc is not None or self._gooya_real_pid is not None:
                self.unload_gooya()
        except Exception:
            pass

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
                and not self._preloading
                and not os.environ.get('CELESTE_NO_AUTO_PRELOAD')
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

    def _resolve_gooya_ref_path(self, cfg: dict) -> str:
        ref_path = cfg.get('gooya_reference_audio') or str(
            PROJECT_ROOT / 'Data' / 'Models' / 'gooya' / 'speaker_reference_clean.wav'
        )
        p = Path(ref_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return str(p)

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
                'gooya_worker_pid': self._gooya_real_pid if worker_alive else None,
                'gooya_loaded': self._gooya_loaded and worker_alive,
                'gooya_precision': self._config.get('gooya_precision', 'bf16'),
                'gooya_auto_unload': self._config.get('gooya_auto_unload', False),
                'gooya_chunking_enabled': bool(self._config.get('gooya_chunking_enabled', True)),
                'gooya_cfm_timesteps': int(self._config.get('gooya_cfm_timesteps', 8)),
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
        # Preserve paragraph/line breaks as sentence separators while normalizing horizontal spaces
        text = re.sub(r'[ \t\r]+', ' ', text)
        text = re.sub(r'\n\s*', '\n', text).strip()
        return explicit_tone or 'صمیمی و طبیعی', text

    # ------------------------------------------------------------------
    # Adaptive 2-Tier Persian/English Chunker (Low TTFA + Natural Prosody)
    # ------------------------------------------------------------------
    @staticmethod
    def _split_atomic_sentences(text: str) -> List[str]:
        """Split text into sentences without breaking decimals (3.14, ۴۴.۵) or abbreviations."""
        sentences: List[str] = []
        digit_chars = set('0123456789۰۱۲۳۴۵۶۷۸۹')
        end_marks = set('.!?؟؛…')

        for raw_line in (text or '').split('\n'):
            line = re.sub(r'\s+', ' ', raw_line).strip()
            if not line:
                continue
            start = 0
            n = len(line)
            i = 0
            while i < n:
                ch = line[i]
                if ch in end_marks:
                    # Protect decimal numbers (e.g., 3.14 or ۴۴.۵)
                    if ch == '.' and i > 0 and (i + 1) < n and line[i - 1] in digit_chars and line[i + 1] in digit_chars:
                        i += 1
                        continue
                    # Consume consecutive punctuation (e.g., ... or ؟!)
                    j = i
                    while (j + 1) < n and line[j + 1] in end_marks:
                        j += 1
                    # Boundary if followed by whitespace or end-of-line
                    if (j + 1) == n or line[j + 1].isspace():
                        seg = line[start:j + 1].strip()
                        if seg:
                            sentences.append(seg)
                        start = j + 1
                        while start < n and line[start].isspace():
                            start += 1
                        i = start
                        continue
                    i = j + 1
                    continue
                i += 1
            tail = line[start:].strip()
            if tail:
                sentences.append(tail)

        return sentences

    @staticmethod
    def _split_long_segment(seg: str, min_chars: int, max_chars: int) -> List[str]:
        """Split a long sentence cleanly at clause punctuation, Persian/English conjunctions, or spaces."""
        parts: List[str] = []
        rem = seg.strip()
        clause_marks = ('،', ',', ':', '—', '–', ';')
        conjunctions = (' و ', ' که ', ' اما ', ' ولی ', ' تا ', ' چون ', ' یا ', ' زیرا ', ' and ', ' but ', ' because ')

        while len(rem) > max_chars:
            split_pos = -1

            # 1. Clause punctuation within [min_chars, max_chars] (prefer rightmost)
            for idx in range(min(max_chars, len(rem) - 1), min_chars - 1, -1):
                if rem[idx] in clause_marks:
                    split_pos = idx + 1
                    break

            # 1b. Slightly past max_chars (+18) if a natural clause mark sits right there
            if split_pos == -1:
                upper = min(len(rem) - 1, max_chars + 18)
                for idx in range(max_chars + 1, upper + 1):
                    if rem[idx] in clause_marks:
                        split_pos = idx + 1
                        break

            # 2. Natural conjunction boundary within [min_chars, max_chars]
            if split_pos == -1:
                window = rem[:max_chars + 1]
                best_conj = -1
                for conj in conjunctions:
                    pos = window.rfind(conj, min_chars)
                    if pos > best_conj:
                        best_conj = pos
                if best_conj >= min_chars:
                    split_pos = best_conj

            # 3. Nearest whitespace <= max_chars
            if split_pos == -1:
                sp = rem.rfind(' ', min_chars, max_chars + 1)
                if sp != -1:
                    split_pos = sp
                else:
                    sp_after = rem.find(' ', max_chars)
                    split_pos = sp_after if sp_after != -1 else max_chars

            left = rem[:split_pos].strip()
            right = rem[split_pos:].strip()
            if not left:
                break
            parts.append(left)
            rem = right
            if not rem:
                break

        if rem:
            parts.append(rem)
        return parts

    @classmethod
    def split_text_for_streaming_tts(
        cls,
        text: str,
        first_max_chars: int = 75,
        first_min_chars: int = 22,
        chunk_max_chars: int = 140,
        chunk_min_chars: int = 45,
    ) -> List[str]:
        """Adaptive 2-tier splitter: short Chunk 1 for ~1.5-2.5s TTFA, longer subsequent chunks for prosody & VRAM safety."""
        raw = (text or '').strip()
        if not raw:
            return []

        flat = re.sub(r'\s+', ' ', raw).strip()
        if len(flat) <= first_max_chars + 15:
            return [flat]

        sentences = cls._split_atomic_sentences(raw)
        if not sentences:
            return [flat]

        chunks: List[str] = []
        buf = ''

        def _flush_buf() -> None:
            nonlocal buf
            if buf:
                chunks.append(buf)
                buf = ''

        for sent in sentences:
            is_first = (len(chunks) == 0)
            cur_min = first_min_chars if is_first else chunk_min_chars
            cur_max = first_max_chars if is_first else chunk_max_chars

            if not buf:
                if len(sent) <= cur_max:
                    buf = sent
                    # Emit Chunk 1 as soon as a complete natural sentence >= first_min_chars is ready
                    if is_first and len(buf) >= first_min_chars:
                        _flush_buf()
                else:
                    pieces = cls._split_long_segment(sent, cur_min, cur_max)
                    if is_first and pieces:
                        chunks.append(pieces[0])
                        # Re-pack remaining pieces using Tier-2 budget
                        for p in pieces[1:]:
                            if not buf:
                                if len(p) <= chunk_max_chars:
                                    buf = p
                                else:
                                    sub = cls._split_long_segment(p, chunk_min_chars, chunk_max_chars)
                                    chunks.extend(sub[:-1])
                                    buf = sub[-1] if sub else ''
                            elif len(buf) + 1 + len(p) <= chunk_max_chars:
                                buf = f'{buf} {p}'
                            else:
                                _flush_buf()
                                buf = p
                    else:
                        chunks.extend(pieces[:-1])
                        buf = pieces[-1] if pieces else ''
            else:
                if len(buf) + 1 + len(sent) <= cur_max:
                    buf = f'{buf} {sent}'
                    if is_first and len(buf) >= first_min_chars:
                        _flush_buf()
                else:
                    _flush_buf()
                    # Now we are strictly in Tier-2 budget
                    if len(sent) <= chunk_max_chars:
                        buf = sent
                    else:
                        pieces = cls._split_long_segment(sent, chunk_min_chars, chunk_max_chars)
                        chunks.extend(pieces[:-1])
                        buf = pieces[-1] if pieces else ''

        _flush_buf()

        # Merge tiny trailing fragments (<22 chars) so Gooya never receives an isolated 1-2 word tail
        while len(chunks) >= 2 and len(chunks[-1]) < 22:
            merge_limit = (first_max_chars + 25) if len(chunks) == 2 else (chunk_max_chars + 35)
            if len(chunks[-2]) + 1 + len(chunks[-1]) <= merge_limit:
                chunks[-2] = f'{chunks[-2]} {chunks[-1]}'
                chunks.pop()
            else:
                break

        return [c for c in chunks if c.strip()]

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
    @staticmethod
    def _is_pid_alive(pid: Optional[int]) -> bool:
        if not pid or pid <= 0:
            return False
        try:
            import psutil
            return psutil.pid_exists(int(pid))
        except Exception:
            try:
                os.kill(int(pid), 0)
                return True
            except OSError:
                return False

    @staticmethod
    def _kill_pid_tree(pid: Optional[int]) -> None:
        if not pid or pid <= 0:
            return
        if os.name == 'nt':
            try:
                subprocess.run(
                    ['taskkill', '/PID', str(int(pid)), '/T', '/F'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                    timeout=5.0,
                )
            except Exception as exc:
                logger.warning('taskkill /PID %s /T /F warning: %s', pid, exc)
        else:
            try:
                os.kill(int(pid), 9)
            except Exception:
                pass

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
            self._gooya_real_pid = None
            if not GOOYA_PYTHON_EXE.exists():
                raise FileNotFoundError(f'Gooya Python interpreter not found: {GOOYA_PYTHON_EXE}')
            if not GOOYA_WORKER_SCRIPT.exists():
                raise FileNotFoundError(f'Gooya worker script not found: {GOOYA_WORKER_SCRIPT}')

            worker_env = os.environ.copy()
            worker_env.update({
                'HF_HUB_OFFLINE': '1',
                'TRANSFORMERS_OFFLINE': '1',
                'HF_HUB_DISABLE_TELEMETRY': '1',
                'TQDM_DISABLE': '1',
                'PYTHONWARNINGS': 'ignore',
                'PYTHONUTF8': '1',
                'PYTORCH_CUDA_ALLOC_CONF': 'max_split_size_mb:128,garbage_collection_threshold:0.7',
            })

            logger.info('Spawning Gooya worker process (offline=1, tqdm=off): %s', GOOYA_WORKER_SCRIPT)
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
                env=worker_env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            self._gooya_proc = proc
            self._gooya_resp_queue = resp_q

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
                    if msg.get('pid'):
                        self._gooya_real_pid = int(msg['pid'])
                    break

            if not ready:
                rc = proc.poll()
                launcher_pid = proc.pid
                real_pid = self._gooya_real_pid
                self._gooya_proc = None
                self._gooya_real_pid = None
                self._kill_pid_tree(launcher_pid)
                if real_pid and real_pid != launcher_pid:
                    self._kill_pid_tree(real_pid)
                raise RuntimeError(f'Gooya worker failed to start within 30s timeout (returncode={rc})')

            logger.info(
                'Gooya worker process is ready and listening (launcher_pid=%d, worker_pid=%s)',
                proc.pid, self._gooya_real_pid or proc.pid,
            )
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
        """Spawn Gooya worker and preload model weights + speaker reference + CUDA warmup into RTX 3080 VRAM."""
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
                        ref_path = self._resolve_gooya_ref_path(cfg)
                        exaggeration = float(cfg.get('gooya_exaggeration', 0.5))
                        warmup = bool(cfg.get('gooya_warmup_on_load', True))

                        req = {
                            'command': 'load',
                            'model_dir': model_dir,
                            'precision': precision,
                            'ref_audio': ref_path,
                            'exaggeration': exaggeration,
                            'warmup': warmup,
                            'turn_id': get_turn_id(),
                        }
                        logger.info('Preloading Gooya Bozorg v1.5 into VRAM (precision=%s, warmup=%s)...', precision, warmup)
                        with self._worker_lock:
                            proc.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
                            proc.stdin.flush()
                            resp = self._read_worker_response(proc, timeout=120.0)

                        if resp.get('status') == 'ok':
                            with self._lock:
                                self._gooya_loaded = True
                                if resp.get('pid'):
                                    self._gooya_real_pid = int(resp['pid'])
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

    def _synthesize_gooya(
        self,
        text: str,
        cfg: dict,
        defer_auto_unload: bool = False,
        chunk_index: int = 1,
        total_chunks: int = 1,
    ) -> Tuple[Optional[np.ndarray], int]:
        t0 = time.perf_counter()
        try:
            proc = self._ensure_gooya_worker()
            ref_path = self._resolve_gooya_ref_path(cfg)
            chunk_info = f'{chunk_index}/{total_chunks}' if total_chunks > 1 else ''

            req = {
                'command': 'synthesize',
                'text': text,
                'ref_audio': ref_path,
                'exaggeration': float(cfg.get('gooya_exaggeration', 0.5)),
                'cfg_weight': float(cfg.get('gooya_cfg_weight', 0.5)),
                'precision': cfg.get('gooya_precision', 'bf16'),
                'temperature': float(cfg.get('gooya_temperature', 0.75)),
                'repetition_penalty': float(cfg.get('gooya_repetition_penalty', 1.25)),
                'cfm_timesteps': int(cfg.get('gooya_cfm_timesteps', 8)),
                'vram_trim_threshold_mb': float(cfg.get('gooya_vram_trim_mb', 2450.0)),
                'trim_silence': bool(cfg.get('gooya_trim_silence', True)),
                'chunk_info': chunk_info,
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
                    'audio_duration_s': float(resp.get('audio_duration_s', 0.0) or 0.0),
                    'rtf': float(resp.get('rtf', 0.0) or 0.0),
                    'vram_allocated_mb': resp.get('vram_allocated_mb', '?'),
                    'vram_reserved_mb': resp.get('vram_reserved_mb', '?'),
                })
                chunk_tag = f' [chunk {chunk_info}]' if chunk_info else ''
                logger.info(
                    'Gooya TTS OK%s | audio=%ss tokens=%s cfm=%s | synth=%.1fms (conds=%sms t3=%sms s3gen=%sms) dsp=%.1fms RTF=%s | VRAM=%s/%s MB',
                    chunk_tag,
                    resp.get('audio_duration_s', '?'),
                    resp.get('speech_tokens', '?'),
                    resp.get('cfm_timesteps', '?'),
                    synth_ms,
                    resp.get('conds_ms', '?'),
                    resp.get('t3_ms', '?'),
                    resp.get('s3gen_ms', '?'),
                    dsp_ms,
                    resp.get('rtf', '?'),
                    resp.get('vram_allocated_mb', '?'),
                    resp.get('vram_reserved_mb', '?'),
                )

                if cfg.get('gooya_auto_unload', False) and not defer_auto_unload:
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
                self._gooya_real_pid = None
                self._gooya_loaded = False

        return None, self.DEFAULT_SAMPLE_RATE

    def unload_gooya(self) -> None:
        """Dynamic VRAM Management: cleanly terminates Gooya launcher AND CUDA child process tree."""
        with self._worker_lock:
            self._gooya_loaded = False
            proc = self._gooya_proc
            launcher_pid = proc.pid if proc is not None else None
            real_pid = self._gooya_real_pid

            if proc is not None:
                try:
                    if proc.poll() is None and proc.stdin is not None:
                        proc.stdin.write(json.dumps({'command': 'exit', 'turn_id': get_turn_id()}) + '\n')
                        proc.stdin.flush()
                        proc.wait(timeout=4.0)
                except Exception as exc:
                    logger.warning('Gooya graceful exit timed out/failed (%s); killing process tree', exc)

                if proc.poll() is None:
                    self._kill_pid_tree(launcher_pid)

            if real_pid and real_pid != launcher_pid and self._is_pid_alive(real_pid):
                logger.warning('Gooya child CUDA worker (PID=%d) still alive after shim exit; forcing taskkill /T /F', real_pid)
                self._kill_pid_tree(real_pid)

            self._gooya_proc = None
            self._gooya_real_pid = None
            if launcher_pid or real_pid:
                logger.info(
                    '[Gooya VRAM Manager] Worker process tree terminated (launcher_pid=%s, worker_pid=%s) and VRAM released.',
                    launcher_pid, real_pid,
                )

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
    def _build_chunks_for_gooya(self, clean_text: str, cfg: dict) -> List[str]:
        if not bool(cfg.get('gooya_chunking_enabled', True)):
            return [re.sub(r'\s+', ' ', clean_text).strip()]
        return self.split_text_for_streaming_tts(
            clean_text,
            first_max_chars=int(cfg.get('gooya_first_chunk_max_chars', 75)),
            first_min_chars=int(cfg.get('gooya_first_chunk_min_chars', 22)),
            chunk_max_chars=int(cfg.get('gooya_chunk_max_chars', 140)),
            chunk_min_chars=int(cfg.get('gooya_chunk_min_chars', 45)),
        )

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
                pcm, sr = self._synthesize_gemini(re.sub(r'\s+', ' ', clean_text).strip(), used_tone, cfg)
                if pcm is not None and len(pcm) > 0:
                    return pcm, sr
                reason = self._last_synth_meta.get('gemini_error', 'gemini_unavailable')
                self._last_synth_meta['fallback_triggered'] = True
                self._last_synth_meta['fallback_reason'] = reason
                logger.warning('TTS FALLBACK triggered: gemini -> gooya (reason=%s)', reason)
                return self._synthesize_gooya(re.sub(r'\s+', ' ', clean_text).strip(), cfg)
            else:
                chunks = self._build_chunks_for_gooya(clean_text, cfg)
                if len(chunks) <= 1:
                    pcm, sr = self._synthesize_gooya(chunks[0], cfg)
                    if pcm is not None and len(pcm) > 0:
                        return pcm, sr
                    reason = self._last_synth_meta.get('gooya_error', 'gooya_unavailable')
                    self._last_synth_meta['fallback_triggered'] = True
                    self._last_synth_meta['fallback_reason'] = reason
                    logger.warning('TTS FALLBACK triggered: gooya -> gemini (reason=%s)', reason)
                    return self._synthesize_gemini(chunks[0], used_tone, cfg)

                # Chunked non-streaming synthesis (protects VRAM for direct synthesize() callers)
                pcm_parts: List[np.ndarray] = []
                out_sr = self.DEFAULT_SAMPLE_RATE
                total_synth_ms = 0.0
                total_dsp_ms = 0.0
                total_audio_s = 0.0
                try:
                    for idx, ch in enumerate(chunks, 1):
                        pcm, sr = self._synthesize_gooya(ch, cfg, defer_auto_unload=True, chunk_index=idx, total_chunks=len(chunks))
                        if pcm is not None and len(pcm) > 0:
                            pcm_parts.append(pcm)
                            out_sr = sr
                            total_synth_ms += float(self._last_synth_meta.get('synth_ms', 0.0))
                            total_dsp_ms += float(self._last_synth_meta.get('dsp_ms', 0.0))
                            total_audio_s += float(self._last_synth_meta.get('audio_duration_s', 0.0))
                        else:
                            reason = self._last_synth_meta.get('gooya_error', 'gooya_unavailable')
                            self._last_synth_meta['fallback_triggered'] = True
                            self._last_synth_meta['fallback_reason'] = reason
                            logger.warning('TTS FALLBACK triggered (chunk %d/%d): gooya -> gemini (reason=%s)', idx, len(chunks), reason)
                            fb_text = ' '.join(chunks[idx - 1:])
                            fb_pcm, fb_sr = self._synthesize_gemini(fb_text, used_tone, cfg)
                            if fb_pcm is not None and len(fb_pcm) > 0:
                                pcm_parts.append(fb_pcm)
                                out_sr = fb_sr
                                total_synth_ms += float(self._last_synth_meta.get('synth_ms', 0.0))
                                total_dsp_ms += float(self._last_synth_meta.get('dsp_ms', 0.0))
                                total_audio_s += float(self._last_synth_meta.get('audio_duration_s', 0.0))
                            break
                finally:
                    if cfg.get('gooya_auto_unload', False):
                        self.unload_gooya()

                if pcm_parts:
                    merged = np.concatenate(pcm_parts)
                    self._last_synth_meta.update({
                        'synth_ms': round(total_synth_ms, 1),
                        'dsp_ms': round(total_dsp_ms, 1),
                        'audio_duration_s': round(total_audio_s, 2),
                    })
                    return merged, out_sr
                return None, self.DEFAULT_SAMPLE_RATE

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

    def _speak_gooya_pipelined(
        self,
        chunks: List[str],
        clean_text: str,
        tone: str,
        cfg: dict,
        gen_id: int,
        t_total: float,
    ) -> bool:
        """Producer-Consumer pipelined Gooya playback: Chunk 1 plays immediately while subsequent chunks synthesize."""
        caller_tid = get_turn_id()
        chunk_q: queue.Queue = queue.Queue(maxsize=4)
        total_chunks = len(chunks)

        def _is_cancelled() -> bool:
            return gen_id != self._speech_generation_id

        def _put_cancellable(item: tuple) -> bool:
            while not _is_cancelled():
                try:
                    chunk_q.put(item, timeout=0.05)
                    return True
                except queue.Full:
                    continue
            return False

        def _producer() -> None:
            with turn_context(caller_tid if caller_tid != '-' else None, prefix='TTS'):
                try:
                    for idx, ch_text in enumerate(chunks, 1):
                        if _is_cancelled():
                            return
                        pcm, sr = self._synthesize_gooya(
                            ch_text,
                            cfg,
                            defer_auto_unload=True,
                            chunk_index=idx,
                            total_chunks=total_chunks,
                        )
                        if _is_cancelled():
                            return
                        if pcm is not None and len(pcm) > 0:
                            meta_snap = dict(self._last_synth_meta)
                            if not _put_cancellable(('audio', idx, pcm, sr, meta_snap)):
                                return
                        else:
                            reason = str(self._last_synth_meta.get('gooya_error', 'gooya_unavailable'))
                            logger.warning(
                                'TTS FALLBACK triggered (chunk %d/%d): gooya -> gemini (reason=%s)',
                                idx, total_chunks, reason,
                            )
                            remaining_text = ' '.join(chunks[idx - 1:])
                            fb_pcm, fb_sr = self._synthesize_gemini(remaining_text, tone, cfg)
                            if _is_cancelled():
                                return
                            meta_snap = dict(self._last_synth_meta)
                            meta_snap['fallback_triggered'] = True
                            meta_snap['fallback_reason'] = reason
                            if fb_pcm is not None and len(fb_pcm) > 0:
                                _put_cancellable(('fallback_audio', idx, fb_pcm, fb_sr, meta_snap))
                            else:
                                _put_cancellable(('error', idx, None, self.DEFAULT_SAMPLE_RATE, meta_snap))
                            return
                except Exception as exc:
                    logger.exception('Gooya chunk producer error: %s', exc)
                    _put_cancellable(('error', 0, None, self.DEFAULT_SAMPLE_RATE, {'gooya_error': str(exc)}))
                finally:
                    if cfg.get('gooya_auto_unload', False):
                        try:
                            self.unload_gooya()
                        except Exception:
                            pass
                    if not _is_cancelled():
                        _put_cancellable(('done', 0, None, self.DEFAULT_SAMPLE_RATE, {}))

        logger.info(
            'Gooya Pipelined TTS start | chars=%d chunks=%d sizes=%s',
            len(clean_text), total_chunks, [len(c) for c in chunks],
        )
        prod_thread = threading.Thread(target=_producer, daemon=True, name='GooyaChunkProducer')
        prod_thread.start()

        chunks_played = 0
        ttfa_ms = 0.0
        total_synth_ms = 0.0
        total_dsp_ms = 0.0
        total_play_ms = 0.0
        total_audio_s = 0.0
        used_engine = 'gooya'
        fallback_triggered = False
        fallback_reason = ''
        interrupted = False

        while True:
            if _is_cancelled():
                interrupted = True
                break
            try:
                kind, idx, pcm, sr, meta = chunk_q.get(timeout=0.05)
            except queue.Empty:
                if not prod_thread.is_alive() and chunk_q.empty():
                    break
                continue

            if kind == 'done':
                break
            if kind == 'error':
                fallback_triggered = bool(meta.get('fallback_triggered', fallback_triggered))
                fallback_reason = str(meta.get('fallback_reason') or meta.get('gooya_error') or 'unknown')
                break

            if _is_cancelled():
                interrupted = True
                break

            if kind == 'fallback_audio':
                used_engine = 'gemini' if chunks_played == 0 else 'gooya+gemini'
                fallback_triggered = True
                fallback_reason = str(meta.get('fallback_reason', ''))

            ch_synth = float(meta.get('synth_ms', 0.0))
            ch_dsp = float(meta.get('dsp_ms', 0.0))
            ch_audio = float(meta.get('audio_duration_s', 0.0))
            total_synth_ms += ch_synth
            total_dsp_ms += ch_dsp
            total_audio_s += ch_audio

            if chunks_played == 0:
                ttfa_ms = round((time.perf_counter() - t_total) * 1000.0, 1)
                logger.info(
                    'Gooya Pipelined TTFA | chunk=1/%d | ttfa=%.1fms (chunk1_synth=%.1fms audio=%.2fs)',
                    total_chunks, ttfa_ms, ch_synth, ch_audio,
                )

            play_ms = self._playback_audio(pcm, sample_rate=sr, blocking=True)
            total_play_ms += play_ms
            chunks_played += 1

            if _is_cancelled():
                interrupted = True
                break

        prod_thread.join(timeout=0.5)
        total_ms = round((time.perf_counter() - t_total) * 1000.0, 1)

        if interrupted:
            logger.info(
                'TTS Pipelined Interrupted (stop/barge-in) | played=%d/%d | ttfa=%.1fms total=%.1fms',
                chunks_played, total_chunks, ttfa_ms, total_ms,
            )
            return False

        if chunks_played > 0:
            logger.info(
                'TTS Turn Complete | req=gooya used=%s fallback=%s%s | chunks=%d/%d | ttfa=%.1fms | chars=%d audio=%.2fs | synth=%.1fms dsp=%.1fms play=%.1fms total=%.1fms',
                used_engine,
                fallback_triggered,
                f' ({fallback_reason})' if fallback_triggered else '',
                chunks_played,
                total_chunks,
                ttfa_ms,
                len(clean_text),
                total_audio_s,
                total_synth_ms,
                total_dsp_ms,
                total_play_ms,
                total_ms,
            )
            return True

        logger.error(
            'TTS Turn Failed (no audio produced) | req=gooya fallback=%s reason=%s | chars=%d',
            fallback_triggered,
            fallback_reason or 'unknown',
            len(clean_text),
        )
        return False

    def _speak_worker(self, clean_text: str, tone: str, gen_id: int, turn_id: Optional[str] = None) -> bool:
        with turn_context(turn_id if turn_id and turn_id != '-' else None, prefix='TTS'):
            t_total = time.perf_counter()
            with self._play_lock:
                with self._lock:
                    if gen_id != self._speech_generation_id:
                        logger.debug('Skipped superseded TTS utterance (gen_id=%d)', gen_id)
                        return False
                    self._speaking = True
                    cfg = dict(self._config)
                    mode = self._engine_mode

                try:
                    # Fast Producer-Consumer streaming path for multi-sentence Gooya replies
                    if mode == 'gooya':
                        chunks = self._build_chunks_for_gooya(clean_text, cfg)
                        if len(chunks) > 1:
                            return self._speak_gooya_pipelined(chunks, clean_text, tone, cfg, gen_id, t_total)

                    # Single-shot path (Gemini or short single-chunk Gooya utterance)
                    pcm, sr = self.synthesize(clean_text, tone=tone, turn_id=get_turn_id())
                    meta = dict(self._last_synth_meta)
                    if pcm is not None and len(pcm) > 0 and gen_id == self._speech_generation_id:
                        ttfa_ms = round((time.perf_counter() - t_total) * 1000.0, 1)
                        play_ms = self._playback_audio(pcm, sample_rate=sr, blocking=True)
                        total_ms = round((time.perf_counter() - t_total) * 1000.0, 1)
                        logger.info(
                            'TTS Turn Complete | req=%s used=%s fallback=%s%s | chunks=1/1 | ttfa=%.1fms | chars=%d audio=%ss | synth=%.1fms dsp=%.1fms play=%.1fms total=%.1fms',
                            meta.get('engine_requested', '?'),
                            meta.get('engine_used', '?'),
                            meta.get('fallback_triggered', False),
                            f" ({meta.get('fallback_reason')})" if meta.get('fallback_triggered') else '',
                            ttfa_ms,
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
    print('C.E.L.E.S.T.E. TTSEngine Self-Test (Dual-Engine + Pipelined Gooya)')
    print('=' * 60)

    t_start = time.time()
    engine = TTSEngine(auto_preload=False)
    engine.preload_gooya = lambda blocking=False: True
    status = engine.get_status()
    print(f'Initialization: OK | Engine Mode: {status["engine_mode"]}')
    assert status['initialized'] is True

    test_raw = 'سلام [لحن: شاد و پرانرژی] من C.E.L.E.S.T.E. هستم! <think>thought</think><think>unclosed'
    tone, clean = engine.clean_text_for_tts(test_raw)
    print(f'Clean text: "{clean}" | Tone: "{tone}"')
    assert 'سلست' in clean and '<think>' not in clean and 'unclosed' not in clean
    assert tone == 'شاد و پرانرژی'

    # Test 2-tier Persian/English chunker
    sample_literary = (
        'در سکوت شب، باد آرام از میان درختان می‌گذشت و برگ‌ها را به رقص درمی‌آورد. '
        'ماه کامل در آسمان صاف می‌درخشید و نور نقره‌ای خود را بر زمین می‌پاشید. '
        'صدای جویبار از دور به گوش می‌رسید و آرامشی عمیق در دل شب جاری بود. '
        'همه چیز در خوابی شیرین فرو رفته بود و زمان گویی ایستاده بود.'
    )
    chunks = engine.split_text_for_streaming_tts(sample_literary)
    print(f'Chunker test: {len(chunks)} chunks -> lengths {[len(c) for c in chunks]}')
    assert len(chunks) >= 3
    assert len(chunks[0]) <= 80

    # Ensure decimals are never split
    decimal_text = 'قیمت امروز ۴۴.۵ تومان و نسخه سیستم 2.0 است. آیا عملیات را شروع کنم مهندس؟'
    dec_chunks = engine.split_text_for_streaming_tts(decimal_text, first_max_chars=50)
    assert any('۴۴.۵' in c for c in dec_chunks) and any('2.0' in c for c in dec_chunks)

    # Test Producer-Consumer pipeline with mock Gooya & mock playback
    engine.set_engine_mode('gooya')
    played_chunks = []
    engine._synthesize_gooya = lambda text, cfg, defer_auto_unload=False, chunk_index=1, total_chunks=1: (
        engine._last_synth_meta.update({'engine_used': 'gooya', 'synth_ms': 25.0, 'dsp_ms': 1.0, 'audio_duration_s': 0.5}) or
        (np.zeros(12000, dtype=np.float32), 24000)
    )
    engine._playback_audio = lambda pcm, sample_rate=24000, blocking=True: (played_chunks.append(len(pcm)) or 10.0)
    ok = engine.speak(sample_literary, blocking=True)
    assert ok is True and len(played_chunks) == len(chunks)
    print(f'Pipelined Gooya mock test: OK ({len(played_chunks)} chunks played seamlessly)')

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