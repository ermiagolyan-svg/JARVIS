# V.E.S.P.E.R. Voice - Dual-Engine TTS (Gemini + Gooya Bozorg v1.5) [Phase 1.5]
# Low-Latency Streaming & Prosody-First Architecture:
#   - Dynamic Runway Scheduler (Just-In-Time chunk sizing from live playback buffer + EWMA RTF)
#   - Sentence-first Persian/English segmentation (preserves full-sentence cadence & decimals)
#   - Gapless continuous sounddevice OutputStream playback (<40ms barge-in) with winsound fallback
#   - Clean Gemini fallback (chunk 1 or remaining tail), Windows venv process-tree cleanup,
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
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import requests

try:
    import sounddevice as _sd
except Exception:
    _sd = None

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


class _ContinuousAudioPlayer:
    """Gapless sounddevice OutputStream player with <40ms barge-in abort."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stream: Any = None
        self._sr: int = 0
        self._aborted: bool = False

    def _ensure_stream(self, sample_rate: int) -> Any:
        if _sd is None:
            return None
        with self._lock:
            if self._aborted:
                return None
            if self._stream is not None and self._sr == sample_rate:
                return self._stream
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            stream = _sd.OutputStream(samplerate=sample_rate, channels=1, dtype='float32')
            stream.start()
            self._stream = stream
            self._sr = sample_rate
            return stream

    def write_wakeup_slice(
        self,
        sample_rate: int,
        slice_ms: int = 40,
        is_cancelled: Optional[Callable[[], bool]] = None,
        ramp_in: bool = False,
    ) -> int:
        if (is_cancelled and is_cancelled()) or self._aborted:
            return 0
        stream = self._ensure_stream(sample_rate)
        if stream is None:
            raise RuntimeError('sounddevice stream unavailable')
        n_samples = max(256, int(round(sample_rate * (slice_ms / 1000.0))))
        sig = TTSEngine._make_wakeup_signal(n_samples, sample_rate=sample_rate, ramp_in=ramp_in)
        block = np.ascontiguousarray(sig, dtype=np.float32).reshape(-1, 1)
        try:
            stream.write(block)
        except Exception:
            if not ((is_cancelled and is_cancelled()) or self._aborted):
                raise
            return 0
        return slice_ms

    def play_chunk(self, pcm: np.ndarray, sample_rate: int, is_cancelled: Callable[[], bool]) -> float:
        if pcm is None or len(pcm) == 0 or is_cancelled():
            return 0.0
        t0 = time.perf_counter()
        stream = self._ensure_stream(sample_rate)
        if stream is None:
            raise RuntimeError('sounddevice stream unavailable')

        data = np.clip(pcm, -1.0, 1.0).astype(np.float32, copy=False)
        step = max(256, int(sample_rate * 0.04))  # 40ms slices for instant barge-in responsiveness
        n = data.size
        pos = 0
        try:
            while pos < n:
                if is_cancelled() or self._aborted:
                    break
                block = np.ascontiguousarray(data[pos:pos + step], dtype=np.float32).reshape(-1, 1)
                stream.write(block)
                pos += step
        except Exception:
            if not (is_cancelled() or self._aborted):
                raise
        return round((time.perf_counter() - t0) * 1000.0, 1)

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            stream = self._stream
            self._stream = None
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def close(self, aborted: bool = False) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is not None:
            try:
                if aborted or self._aborted:
                    stream.abort()
                else:
                    stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass


class TTSEngine:
    """C.E.L.E.S.T.E. Dual-Engine Speech Synthesis (Gemini REST + Dynamic Pipelined Gooya Bozorg v1.5)."""

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
        self._active_player: Optional[_ContinuousAudioPlayer] = None
        self._queue = []
        self._dsp = None
        self._gooya_proc: Optional[subprocess.Popen] = None
        self._gooya_real_pid: Optional[int] = None
        self._gooya_resp_queue: queue.Queue = queue.Queue()
        self._gooya_loaded: bool = False
        self._preloading: bool = False
        self._last_synth_meta: Dict[str, Any] = {}
        # Adaptive live telemetry estimates for Dynamic Runway Scheduler (calibrated for RTX 3080 Gooya)
        self._ewma_sec_per_char: float = 0.074
        self._ewma_rtf: float = 0.60
        self._config = self._load_config()
        self._engine_mode = self._config.get('engine_mode', 'gemini').lower()
        if self._engine_mode not in ('gemini', 'gooya'):
            self._engine_mode = 'gemini'
        self._initialized = True
        with _SINGLETON_LOCK:
            if _SINGLETON_INSTANCE is None:
                _SINGLETON_INSTANCE = self

        atexit.register(self._atexit_cleanup)
        logger.info('TTSEngine initialized with mode: %s (continuous_audio=%s)', self._engine_mode, _sd is not None)

        if (
            auto_preload
            and not os.environ.get('VESPER_NO_AUTO_PRELOAD')
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
                and not os.environ.get('VESPER_NO_AUTO_PRELOAD')
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
                'gooya_cfm_timesteps': int(self._config.get('gooya_cfm_timesteps', 10)),
                'continuous_audio_available': _sd is not None,
                'audio_preroll_ms': self._get_audio_preroll_ms(self._config),
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
        text = re.sub(r'V\s*\.\s*E\s*\.\s*S\s*\.\s*P\s*\.\s*E\s*\.\s*R\.?', 'وِسپِر', text, flags=re.IGNORECASE)
        text = re.sub(r'\bVESPER\b', 'وِسپِر', text, flags=re.IGNORECASE)
        tone_match = re.search(r'\[\s*(?:لحن|حس|tone)\s*:\s*([^\]]+)\]', text, flags=re.IGNORECASE)
        explicit_tone = ''
        if tone_match:
            explicit_tone = tone_match.group(1).strip()
            text = text[:tone_match.start()] + text[tone_match.end():]
        text = re.sub(r'[*#_~>|•]', ' ', text)
        text = re.sub(r'[ \t\r]+', ' ', text)
        text = re.sub(r'\n\s*', '\n', text).strip()
        return explicit_tone or 'صمیمی و طبیعی', text

    # ------------------------------------------------------------------
    # Sentence-First Persian/English Segmentation & Dynamic Runway Planner
    # ------------------------------------------------------------------
    @staticmethod
    def _split_atomic_sentences(text: str) -> List[str]:
        """Split text into natural sentences without breaking decimals (3.14, ۴۴.۵)."""
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
                    if ch == '.' and i > 0 and (i + 1) < n and line[i - 1] in digit_chars and line[i + 1] in digit_chars:
                        i += 1
                        continue
                    j = i
                    while (j + 1) < n and line[j + 1] in end_marks:
                        j += 1
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

    @classmethod
    def _pop_prefix_from_segment(cls, seg: str, min_chars: int, max_chars: int) -> Tuple[str, str]:
        """Split off a clean clause prefix <= max_chars from an oversized sentence, preserving the rest intact."""
        rem = seg.strip()
        if len(rem) <= max_chars:
            return rem, ''

        clause_marks = ('،', ',', ':', '—', '–', ';', '؛')
        conjunctions = (' و ', ' که ', ' اما ', ' ولی ', ' تا ', ' چون ', ' یا ', ' زیرا ', ' and ', ' but ', ' because ')
        eff_min = max(12, min(min_chars, max_chars - 10))
        split_pos = -1

        # 1. Clause punctuation inside [eff_min, max_chars] (rightmost)
        for idx in range(min(max_chars, len(rem) - 1), eff_min - 1, -1):
            if rem[idx] in clause_marks:
                split_pos = idx + 1
                break

        # 1b. Slightly past max_chars (+16) if a natural clause mark sits right there
        if split_pos == -1:
            upper = min(len(rem) - 1, max_chars + 16)
            for idx in range(max_chars + 1, upper + 1):
                if rem[idx] in clause_marks:
                    split_pos = idx + 1
                    break

        # 2. Natural conjunction boundary
        if split_pos == -1:
            window = rem[:max_chars + 1]
            best_conj = -1
            for conj in conjunctions:
                pos = window.rfind(conj, eff_min)
                if pos > best_conj:
                    best_conj = pos
            if best_conj >= eff_min:
                split_pos = best_conj

        # 3. Nearest whitespace
        if split_pos == -1:
            sp = rem.rfind(' ', eff_min, max_chars + 1)
            if sp != -1:
                split_pos = sp
            else:
                sp_after = rem.find(' ', max_chars)
                split_pos = sp_after if sp_after != -1 else max_chars

        left = rem[:split_pos].strip()
        right = rem[split_pos:].strip()
        if not left:
            cut = min(len(rem), max(1, max_chars))
            return rem[:cut].strip(), rem[cut:].strip()

        # Prevent tiny orphan tail (<24 chars)
        if right and len(right) < 24 and len(rem) <= max_chars + 24:
            return rem, ''

        return left, right

    @classmethod
    def _pop_next_dynamic_chunk(
        cls,
        remaining: List[str],
        target_max: int,
        target_min: int,
        vram_max: int,
        is_first: bool,
    ) -> Tuple[str, bool]:
        """Pop the next prosody-optimal chunk from remaining sentences given live target_max budget."""
        if not remaining:
            return '', False

        sent = remaining.pop(0).strip()
        if not sent:
            return cls._pop_next_dynamic_chunk(remaining, target_max, target_min, vram_max, is_first)

        # Sentence-integrity grace: prefer keeping a full sentence intact if only slightly over budget
        grace_cap = min(vram_max, target_max + 15)
        if len(sent) > grace_cap:
            left, right = cls._pop_prefix_from_segment(sent, target_min, min(target_max, vram_max))
            if right:
                remaining.insert(0, right)
                return left, True
            return left, False

        chunk = sent
        if is_first:
            # Combine tiny opening sentences (< target_min, e.g. 42 chars) to build healthy runway & cadence
            while remaining and len(chunk) < target_min:
                nxt = remaining[0]
                if len(chunk) + 1 + len(nxt) <= min(target_max, vram_max):
                    chunk = f'{chunk} {remaining.pop(0)}'
                else:
                    break
        else:
            pack_cap = min(target_max, vram_max)
            while remaining:
                nxt = remaining[0]
                if len(chunk) + 1 + len(nxt) <= pack_cap:
                    chunk = f'{chunk} {remaining.pop(0)}'
                else:
                    break

        # Merge tiny final tail (<28 chars) so Gooya never speaks an isolated 1-2 word fragment
        if len(remaining) == 1 and len(remaining[0]) < 28:
            tail_cap = min(vram_max + 20, target_max + 25)
            if len(chunk) + 1 + len(remaining[0]) <= tail_cap:
                chunk = f'{chunk} {remaining.pop(0)}'

        return chunk, False

    @classmethod
    def split_text_for_streaming_tts(
        cls,
        text: str,
        first_max_chars: int = 105,
        first_min_chars: int = 42,
        chunk_max_chars: int = 150,
        chunk_min_chars: int = 45,
        single_shot_max_chars: int = 115,
    ) -> List[str]:
        """Static sentence-first splitter (used by direct synthesize() calls & unit tests)."""
        raw = (text or '').strip()
        if not raw:
            return []

        flat = re.sub(r'\s+', ' ', raw).strip()
        if len(flat) <= single_shot_max_chars:
            return [flat]

        rem = cls._split_atomic_sentences(raw)
        if not rem:
            return [flat]

        chunks: List[str] = []
        while rem:
            is_first = (len(chunks) == 0)
            ch, _ = cls._pop_next_dynamic_chunk(
                rem,
                target_max=first_max_chars if is_first else chunk_max_chars,
                target_min=first_min_chars if is_first else chunk_min_chars,
                vram_max=chunk_max_chars,
                is_first=is_first,
            )
            if ch:
                chunks.append(ch)
        return chunks

    def _update_ewma_metrics(self, chars: int, audio_s: float, rtf: float) -> None:
        if chars <= 0 or audio_s <= 0.2 or rtf <= 0.05:
            return
        obs_spc = max(0.035, min(0.14, audio_s / float(chars)))
        obs_rtf = max(0.25, min(1.20, float(rtf)))
        alpha = 0.35
        with self._lock:
            self._ewma_sec_per_char = (1.0 - alpha) * self._ewma_sec_per_char + alpha * obs_spc
            self._ewma_rtf = (1.0 - alpha) * self._ewma_rtf + alpha * obs_rtf

    def _compute_dynamic_char_budget(self, available_runway_s: float, cfg: dict) -> int:
        """Compute max chars for next chunk so synthesis finishes strictly before playback runway ends."""
        vram_max = int(cfg.get('gooya_chunk_max_chars', 150))
        min_floor = int(cfg.get('gooya_chunk_min_chars', 45))
        safety_s = float(cfg.get('gooya_runway_safety_s', 0.40))
        fixed_overhead_s = 0.30

        with self._lock:
            spc = self._ewma_sec_per_char
            rtf = self._ewma_rtf

        synth_rate_per_char = max(0.025, spc * max(rtf, 0.50))
        synth_budget_s = max(1.6, available_runway_s - safety_s - fixed_overhead_s)
        est_chars = int(synth_budget_s / synth_rate_per_char)
        return max(min_floor, min(vram_max, est_chars))

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
                json_prefix='VESPER_LOG:',
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
        is_continuation: bool = False,
    ) -> Tuple[Optional[np.ndarray], int]:
        t0 = time.perf_counter()
        try:
            proc = self._ensure_gooya_worker()
            ref_path = self._resolve_gooya_ref_path(cfg)
            if total_chunks > 1:
                chunk_info = f'{chunk_index}/{total_chunks}'
            elif chunk_index > 1:
                chunk_info = f'{chunk_index}'
            else:
                chunk_info = ''

            req = {
                'command': 'synthesize',
                'text': text,
                'ref_audio': ref_path,
                'exaggeration': float(cfg.get('gooya_exaggeration', 0.5)),
                'cfg_weight': float(cfg.get('gooya_cfg_weight', 0.5)),
                'precision': cfg.get('gooya_precision', 'bf16'),
                'temperature': float(cfg.get('gooya_temperature', 0.75)),
                'repetition_penalty': float(cfg.get('gooya_repetition_penalty', 1.25)),
                'cfm_timesteps': int(cfg.get('gooya_cfm_timesteps', 10)),
                'vram_trim_threshold_mb': float(cfg.get('gooya_vram_trim_mb', 2450.0)),
                'trim_silence': bool(cfg.get('gooya_trim_silence', True)),
                'is_continuation': bool(is_continuation),
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

                audio_s = float(resp.get('audio_duration_s', 0.0) or 0.0)
                rtf_val = float(resp.get('rtf', 0.0) or 0.0)
                self._update_ewma_metrics(len(text), audio_s, rtf_val)

                self._last_synth_meta.update({
                    'engine_used': 'gooya',
                    'synth_ms': synth_ms,
                    'dsp_ms': dsp_ms,
                    'audio_duration_s': audio_s,
                    'rtf': rtf_val,
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
    @staticmethod
    def _get_audio_preroll_ms(cfg: dict) -> int:
        try:
            val = int(round(float(cfg.get('audio_preroll_ms', 450))))
        except (TypeError, ValueError):
            val = 450
        return max(0, min(1500, val))

    @staticmethod
    def _make_wakeup_signal(num_samples: int, sample_rate: int = 24000, ramp_in: bool = True) -> np.ndarray:
        """Ultra-quiet (-72 dBFS) non-zero dither that wakes HDMI/Bluetooth/DAC mute relays without audible noise."""
        if num_samples <= 0:
            return np.zeros(0, dtype=np.float32)
        rng = np.random.default_rng()
        sig = rng.uniform(-2.5e-4, 2.5e-4, size=num_samples).astype(np.float32)
        if ramp_in:
            fade = min(num_samples, max(1, int(sample_rate * 0.010)))
            if fade > 1:
                sig[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
        return sig

    @classmethod
    def _prepend_preroll(cls, pcm: np.ndarray, sample_rate: int, preroll_ms: int) -> np.ndarray:
        if pcm is None or len(pcm) == 0 or preroll_ms <= 0 or sample_rate <= 0:
            return pcm
        pad_samples = int(round(sample_rate * (preroll_ms / 1000.0)))
        if pad_samples <= 0:
            return pcm
        wakeup = cls._make_wakeup_signal(pad_samples, sample_rate=sample_rate, ramp_in=True)
        return np.concatenate((wakeup, np.asarray(pcm, dtype=np.float32)))

    def _apply_dsp(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        try:
            from Voice.AudioDSP.vesper_dsp import VesperAudioDSP
            if getattr(self, '_dsp', None) is None or self._dsp._sr != sample_rate:
                self._dsp = VesperAudioDSP(sample_rate=sample_rate)
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
            first_max_chars=int(cfg.get('gooya_first_sentence_max_chars', 105)),
            first_min_chars=int(cfg.get('gooya_first_chunk_min_chars', 42)),
            chunk_max_chars=int(cfg.get('gooya_chunk_max_chars', 150)),
            chunk_min_chars=int(cfg.get('gooya_chunk_min_chars', 45)),
            single_shot_max_chars=int(cfg.get('gooya_single_shot_max_chars', 115)),
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

    def _speak_gooya_dynamic_pipelined(
        self,
        sentences: List[str],
        clean_text: str,
        tone: str,
        cfg: dict,
        gen_id: int,
        t_total: float,
    ) -> bool:
        """Dynamic Runway Producer-Consumer Gooya pipeline: zero inter-chunk stalls + full-sentence prosody."""
        caller_tid = get_turn_id()
        chunk_q: queue.Queue = queue.Queue(maxsize=4)
        remaining = list(sentences)

        first_max = int(cfg.get('gooya_first_sentence_max_chars', 105))
        first_min = int(cfg.get('gooya_first_chunk_min_chars', 42))
        vram_max = int(cfg.get('gooya_chunk_max_chars', 150))
        chunk_min = int(cfg.get('gooya_chunk_min_chars', 45))

        runway_lock = threading.Lock()
        runway_state: Dict[str, Any] = {
            'total_produced_s': 0.0,
            'play_start_t': None,
            'planned_sizes': [],
            'total_chunks': 0,
        }

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

        def _get_available_runway_s() -> float:
            with runway_lock:
                prod_s = float(runway_state['total_produced_s'])
                play_t = runway_state['play_start_t']
            if play_t is None:
                return prod_s
            return max(0.0, prod_s - (time.perf_counter() - play_t))

        def _producer() -> None:
            with turn_context(caller_tid if caller_tid != '-' else None, prefix='TTS'):
                idx = 0
                try:
                    while remaining:
                        if _is_cancelled():
                            return
                        idx += 1
                        is_first = (idx == 1)
                        if is_first:
                            target_max = first_max
                            target_min = first_min
                            runway_s = 0.0
                        else:
                            runway_s = _get_available_runway_s()
                            target_max = self._compute_dynamic_char_budget(runway_s, cfg)
                            target_min = chunk_min

                        ch_text, is_cont = self._pop_next_dynamic_chunk(
                            remaining,
                            target_max=target_max,
                            target_min=target_min,
                            vram_max=vram_max,
                            is_first=is_first,
                        )
                        if not ch_text:
                            break

                        with runway_lock:
                            runway_state['planned_sizes'].append(len(ch_text))
                            runway_state['total_chunks'] = idx

                        if idx > 1:
                            logger.debug(
                                'Dynamic Runway Plan | chunk=%d runway=%.2fs -> budget=%d chars, picked=%d chars (cont=%s, rem_sents=%d)',
                                idx, runway_s, target_max, len(ch_text), is_cont, len(remaining),
                            )

                        pcm, sr = self._synthesize_gooya(
                            ch_text,
                            cfg,
                            defer_auto_unload=True,
                            chunk_index=idx,
                            total_chunks=idx if not remaining else 0,
                            is_continuation=is_cont,
                        )
                        if _is_cancelled():
                            return

                        if pcm is not None and len(pcm) > 0:
                            meta_snap = dict(self._last_synth_meta)
                            dur_s = float(meta_snap.get('audio_duration_s', 0.0) or (len(pcm) / float(sr)))
                            with runway_lock:
                                runway_state['total_produced_s'] += dur_s
                            if not _put_cancellable(('audio', idx, pcm, sr, meta_snap)):
                                return
                        else:
                            reason = str(self._last_synth_meta.get('gooya_error', 'gooya_unavailable'))
                            logger.warning(
                                'TTS FALLBACK triggered (chunk %d): gooya -> gemini (reason=%s)',
                                idx, reason,
                            )
                            tail_parts = [ch_text] + remaining
                            remaining.clear()
                            fb_text = ' '.join(tail_parts)
                            fb_pcm, fb_sr = self._synthesize_gemini(fb_text, tone, cfg)
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
                    logger.exception('Gooya dynamic chunk producer error: %s', exc)
                    _put_cancellable(('error', 0, None, self.DEFAULT_SAMPLE_RATE, {'gooya_error': str(exc)}))
                finally:
                    if cfg.get('gooya_auto_unload', False):
                        try:
                            self.unload_gooya()
                        except Exception:
                            pass
                    if not _is_cancelled():
                        _put_cancellable(('done', 0, None, self.DEFAULT_SAMPLE_RATE, {}))

        # Use continuous sounddevice OutputStream unless disabled or mocked in unit tests
        playback_mocked = '_playback_audio' in self.__dict__
        use_continuous = bool(cfg.get('gooya_use_continuous_audio', True)) and (_sd is not None) and (not playback_mocked)
        player: Optional[_ContinuousAudioPlayer] = _ContinuousAudioPlayer() if use_continuous else None
        if player is not None:
            with self._lock:
                self._active_player = player

        preroll_ms = self._get_audio_preroll_ms(cfg)
        logger.info(
            'Gooya Dynamic Pipelined TTS start | chars=%d sentences=%d audio_backend=%s preroll=%dms',
            len(clean_text), len(sentences), 'sounddevice' if use_continuous else 'winsound', preroll_ms,
        )
        prod_thread = threading.Thread(target=_producer, daemon=True, name='GooyaChunkProducer')
        prod_thread.start()

        chunks_played = 0
        ttfa_ms = 0.0
        total_synth_ms = 0.0
        total_dsp_ms = 0.0
        total_play_ms = 0.0
        total_audio_s = 0.0
        total_stall_ms = 0.0
        used_engine = 'gooya'
        fallback_triggered = False
        fallback_reason = ''
        interrupted = False
        last_chunk_end_t: Optional[float] = None
        prime_sr = int(cfg.get('sample_rate', self.DEFAULT_SAMPLE_RATE) or self.DEFAULT_SAMPLE_RATE)
        prime_start_t: Optional[float] = None
        primed_ms = 0
        extra_preroll_ms = preroll_ms
        first_prime_slice = True

        try:
            while True:
                if _is_cancelled():
                    interrupted = True
                    break
                if chunks_played == 0 and player is not None:
                    try:
                        kind, idx, pcm, sr, meta = chunk_q.get_nowait()
                    except queue.Empty:
                        if not prod_thread.is_alive() and chunk_q.empty():
                            break
                        try:
                            if prime_start_t is None:
                                prime_start_t = time.perf_counter()
                            wrote_ms = player.write_wakeup_slice(
                                prime_sr,
                                slice_ms=40,
                                is_cancelled=_is_cancelled,
                                ramp_in=first_prime_slice,
                            )
                            first_prime_slice = False
                            if wrote_ms > 0:
                                primed_ms += wrote_ms
                        except Exception as warm_exc:
                            logger.warning('Continuous sounddevice pre-warm failed (%s); falling back to winsound', warm_exc)
                            player.close(aborted=True)
                            player = None
                            prime_start_t = None
                            primed_ms = 0
                            with self._lock:
                                self._active_player = None
                        continue
                else:
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

                now_t = time.perf_counter()
                pcm_to_play = pcm
                if chunks_played == 0:
                    ttfa_ms = round((now_t - t_total) * 1000.0, 1)
                    if player is not None and prime_start_t is not None and primed_ms > 0 and sr == prime_sr:
                        effective_primed_ms = int(round((now_t - prime_start_t) * 1000.0))
                    else:
                        effective_primed_ms = 0
                    extra_preroll_ms = max(0, preroll_ms - effective_primed_ms)
                    if extra_preroll_ms > 0:
                        pcm_to_play = self._prepend_preroll(pcm, sr, extra_preroll_ms)
                    with runway_lock:
                        runway_state['play_start_t'] = now_t
                        if extra_preroll_ms > 0:
                            runway_state['total_produced_s'] += (extra_preroll_ms / 1000.0)
                    primed_ms = effective_primed_ms
                    logger.info(
                        'Gooya Pipelined TTFA | chunk=1 | ttfa=%.1fms primed=%dms extra_preroll=%dms (chunk1_synth=%.1fms audio=%.2fs)',
                        ttfa_ms, primed_ms, extra_preroll_ms, ch_synth, ch_audio,
                    )
                elif last_chunk_end_t is not None:
                    gap_ms = (now_t - last_chunk_end_t) * 1000.0
                    if gap_ms > 25.0:
                        total_stall_ms += gap_ms

                if player is not None:
                    try:
                        play_ms = player.play_chunk(pcm_to_play, sample_rate=sr, is_cancelled=_is_cancelled)
                    except Exception as sd_exc:
                        logger.warning('Continuous sounddevice playback failed (%s); falling back to winsound', sd_exc)
                        player.close(aborted=True)
                        player = None
                        with self._lock:
                            self._active_player = None
                        play_ms = self._playback_audio(pcm_to_play, sample_rate=sr, blocking=True)
                else:
                    play_ms = self._playback_audio(pcm_to_play, sample_rate=sr, blocking=True)

                last_chunk_end_t = time.perf_counter()
                total_play_ms += play_ms
                chunks_played += 1

                if _is_cancelled():
                    interrupted = True
                    break
        finally:
            if player is not None:
                player.close(aborted=interrupted)
                with self._lock:
                    if self._active_player is player:
                        self._active_player = None

        prod_thread.join(timeout=0.5)
        total_ms = round((time.perf_counter() - t_total) * 1000.0, 1)
        with runway_lock:
            sizes = list(runway_state['planned_sizes'])
            total_chunks = max(chunks_played, int(runway_state['total_chunks']))

        if interrupted:
            logger.info(
                'TTS Pipelined Interrupted (stop/barge-in) | played=%d/%d | ttfa=%.1fms total=%.1fms',
                chunks_played, total_chunks, ttfa_ms, total_ms,
            )
            return False

        if chunks_played > 0:
            logger.info(
                'TTS Turn Complete | req=gooya used=%s fallback=%s%s | chunks=%d/%d sizes=%s | ttfa=%.1fms primed=%dms preroll=%dms stall=%.1fms | chars=%d audio=%.2fs | synth=%.1fms dsp=%.1fms play=%.1fms total=%.1fms',
                used_engine,
                fallback_triggered,
                f' ({fallback_reason})' if fallback_triggered else '',
                chunks_played,
                total_chunks,
                sizes,
                ttfa_ms,
                primed_ms,
                extra_preroll_ms,
                round(total_stall_ms, 1),
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
                    # Dynamic Runway streaming path for multi-sentence / longer Gooya replies
                    if mode == 'gooya' and bool(cfg.get('gooya_chunking_enabled', True)):
                        flat = re.sub(r'\s+', ' ', clean_text).strip()
                        single_shot_max = int(cfg.get('gooya_single_shot_max_chars', 115))
                        if len(flat) > single_shot_max:
                            sentences = self._split_atomic_sentences(clean_text)
                            if sentences:
                                return self._speak_gooya_dynamic_pipelined(sentences, clean_text, tone, cfg, gen_id, t_total)

                    # Single-shot path (Gemini or short <=115-char Gooya utterance for 100% native prosody)
                    pcm, sr = self.synthesize(clean_text, tone=tone, turn_id=get_turn_id())
                    meta = dict(self._last_synth_meta)
                    if pcm is not None and len(pcm) > 0 and gen_id == self._speech_generation_id:
                        preroll_ms = self._get_audio_preroll_ms(cfg)
                        pcm_to_play = self._prepend_preroll(pcm, sr, preroll_ms)
                        ttfa_ms = round((time.perf_counter() - t_total) * 1000.0, 1)
                        playback_mocked = '_playback_audio' in self.__dict__
                        use_continuous = bool(cfg.get('gooya_use_continuous_audio', True)) and (_sd is not None) and (not playback_mocked)
                        if use_continuous:
                            single_player = _ContinuousAudioPlayer()
                            with self._lock:
                                self._active_player = single_player
                            try:
                                play_ms = single_player.play_chunk(
                                    pcm_to_play,
                                    sample_rate=sr,
                                    is_cancelled=lambda: gen_id != self._speech_generation_id,
                                )
                            except Exception as sd_exc:
                                logger.warning('Single-shot sounddevice playback failed (%s); falling back to winsound', sd_exc)
                                single_player.close(aborted=True)
                                play_ms = self._playback_audio(pcm_to_play, sample_rate=sr, blocking=True)
                            else:
                                single_player.close(aborted=(gen_id != self._speech_generation_id))
                            finally:
                                with self._lock:
                                    if self._active_player is single_player:
                                        self._active_player = None
                        else:
                            play_ms = self._playback_audio(pcm_to_play, sample_rate=sr, blocking=True)
                        total_ms = round((time.perf_counter() - t_total) * 1000.0, 1)
                        logger.info(
                            'TTS Turn Complete | req=%s used=%s fallback=%s%s | chunks=1/1 | ttfa=%.1fms preroll=%dms stall=0.0ms | chars=%d audio=%ss | synth=%.1fms dsp=%.1fms play=%.1fms total=%.1fms',
                            meta.get('engine_requested', '?'),
                            meta.get('engine_used', '?'),
                            meta.get('fallback_triggered', False),
                            f" ({meta.get('fallback_reason')})" if meta.get('fallback_triggered') else '',
                            ttfa_ms,
                            preroll_ms,
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
            player = self._active_player
        if player is not None:
            try:
                player.abort()
            except Exception:
                pass
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
    print('C.E.L.E.S.T.E. TTSEngine Self-Test (Phase 1.5 Dynamic Runway)')
    print('=' * 60)

    t_start = time.time()
    engine = TTSEngine(auto_preload=False)
    engine.preload_gooya = lambda blocking=False: True
    status = engine.get_status()
    print(f'Initialization: OK | Engine Mode: {status["engine_mode"]} | ContinuousAudio: {status["continuous_audio_available"]}')
    assert status['initialized'] is True

    test_raw = 'سلام [لحن: شاد و پرانرژی] من V.E.S.P.E.R. هستم! <think>thought</think><think>unclosed'
    tone, clean = engine.clean_text_for_tts(test_raw)
    print(f'Clean text: "{clean}" | Tone: "{tone}"')
    assert 'وسپر' in clean and '<think>' not in clean and 'unclosed' not in clean
    assert tone == 'شاد و پرانرژی'

    # Test 1: Short text (<=115 chars) stays single-shot for 100% prosody
    short_reply = 'دمای پردازنده الان ۴۸ درجه است و همه سیستم‌ها پایدار هستند مهندس.'
    assert len(engine.split_text_for_streaming_tts(short_reply)) == 1

    # Test 2: Sentence-first splitting + decimal preservation
    decimal_text = 'قیمت امروز ۴۴.۵ تومان و نسخه سیستم 2.0 است. آیا عملیات را همین الان شروع کنم مهندس؟ همه چیز برای اجرا کاملاً آماده و تست شده است.'
    dec_chunks = engine.split_text_for_streaming_tts(decimal_text)
    assert any('۴۴.۵' in c for c in dec_chunks) and any('2.0' in c for c in dec_chunks)

    # Test 3: Dynamic Runway Scheduler on 4-sentence literary text
    sample_literary = (
        'در سکوت شب، باد آرام از میان درختان می‌گذشت و برگ‌ها را به رقص درمی‌آورد. '
        'ماه کامل در آسمان صاف می‌درخشید و نور نقره‌ای خود را بر زمین می‌پاشید. '
        'صدای جویبار از دور به گوش می‌رسید و آرامشی عمیق در دل شب جاری بود. '
        'همه چیز در خوابی شیرین فرو رفته بود و زمان گویی ایستاده بود.'
    )
    engine.set_engine_mode('gooya')
    synth_texts: List[str] = []
    played_chunks: List[int] = []

    def _mock_gooya(text, cfg, defer_auto_unload=False, chunk_index=1, total_chunks=1, is_continuation=False):
        synth_texts.append(text)
        est_audio_s = round(len(text) * 0.074, 2)
        engine._update_ewma_metrics(len(text), est_audio_s, 0.58)
        engine._last_synth_meta.update({
            'engine_used': 'gooya',
            'synth_ms': 25.0,
            'dsp_ms': 1.0,
            'audio_duration_s': est_audio_s,
            'rtf': 0.58,
        })
        return np.zeros(12000, dtype=np.float32), 24000

    engine._synthesize_gooya = _mock_gooya
    engine._playback_audio = lambda pcm, sample_rate=24000, blocking=True: (played_chunks.append(len(pcm)) or 10.0)

    ok = engine.speak(sample_literary, blocking=True)
    expected_preroll_samples = int(round(24000 * (engine._get_audio_preroll_ms(engine._config) / 1000.0)))
    print(f'Dynamic Runway mock test: OK | chunks={len(played_chunks)} sizes={[len(t) for t in synth_texts]} played_samples={played_chunks}')
    assert ok is True and len(played_chunks) == len(synth_texts) and len(synth_texts) >= 3
    # Verify preroll applied ONLY to chunk 1 and never to chunks 2..N
    assert played_chunks[0] == 12000 + expected_preroll_samples
    assert all(n == 12000 for n in played_chunks[1:])
    # Verify Chunk 1 preserved the entire first sentence intact (73 chars) without mid-sentence cut!
    assert synth_texts[0].endswith('.')

    # Test 4: Oversized single sentence splits cleanly at Persian comma with continuation=True
    long_single = 'باد پاییزی از میان کوچه‌های قدیمی شهر می‌گذشت، برگ‌های زرد را روی سنگفرش می‌چرخاند، عطر باران را با خود می‌آورد، و یاد خاطرات دور را در دل رهگذران زنده می‌کرد.'
    rem_long = engine._split_atomic_sentences(long_single)
    ch1, is_cont1 = engine._pop_next_dynamic_chunk(rem_long, target_max=105, target_min=42, vram_max=150, is_first=True)
    assert is_cont1 is True and ch1.endswith('،') and len(rem_long) == 1
    print(f'Long single-sentence continuation test: OK | chunk1={len(ch1)} chars (ends with comma), rem={len(rem_long[0])} chars')

    engine.update_config({'engine_mode': 'gemini', 'gemini_voice_name': 'Kore'})
    st_after = engine.get_status()
    assert st_after['engine_mode'] == 'gemini'
    assert st_after['gemini_voice_name'] == 'Kore'
    assert st_after['gooya_worker_alive'] is False

    original_gemini = engine._synthesize_gemini
    engine._synthesize_gemini = lambda text, tone, cfg: (
        engine._apply_dsp(np.zeros(24000, dtype=np.float32), 24000), 24000
    )
    pcm, sr = engine.synthesize('وسپر آماده است.')
    assert pcm is not None and len(pcm) == 24000 and sr == 24000
    engine._synthesize_gemini = original_gemini

    engine.shutdown()
    print(f'All Phase 1.5 self-tests passed in {time.time() - t_start:.2f} seconds.')
    print('=' * 60)