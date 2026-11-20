# C.E.L.E.S.T.E. Voice - Gooya Bozorg v1.5 Isolated Low-Latency Worker
# Runs in Data/Models/gooya/venv (Python 3.11 with torch 2.6.0+cu124)
# Stability & Latency architecture:
#   - Strictly offline (HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1) + TQDM_DISABLE=1
#   - Windows-safe CUDA allocator fragmentation cap (max_split_size_mb:128)
#   - T3 in bfloat16 + SDPA (saves ~1.05 GB VRAM)
#   - S3Gen & VoiceEncoder strictly in float32 + TF32 (zero bf16 token/length crash)
#   - Adaptive n_cfm_timesteps introspection, startup CUDA kernel warmup,
#     conditional VRAM cache trim (<2.45 GB reserved), and click-free chunk audio polish
from __future__ import annotations

import os

# Enforce offline cache, CUDA allocator tuning & disable tqdm/warnings BEFORE importing torch/hf/chatterbox
os.environ['PYTHONWARNINGS'] = 'ignore'
os.environ['TQDM_DISABLE'] = '1'
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ.setdefault('PYTORCH_CUDA_ALLOC_CONF', 'max_split_size_mb:128,garbage_collection_threshold:0.7')

import base64
import gc
import inspect
import json
import logging
import re
import sys
import time
import traceback
import warnings
from pathlib import Path
from typing import Dict, Optional

warnings.filterwarnings('ignore')

try:
    sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
except Exception:
    pass

LOG_PREFIX = 'CELESTE_LOG:'
RESP_PREFIX = 'GOOYA_WORKER_RESP:'
_current_turn_id = '-'


class _JsonStderrHandler(logging.Handler):
    """Emit structured JSON log records to stderr for clean parent-process ingestion."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            tb = ''
            if record.exc_info:
                tb = ''.join(traceback.format_exception(*record.exc_info)).rstrip()
            payload = {
                'level': record.levelname,
                'module': 'gooya_worker',
                'turn_id': _current_turn_id,
                'msg': msg,
                'tb': tb,
            }
            sys.stderr.write(LOG_PREFIX + json.dumps(payload, ensure_ascii=False) + '\n')
            sys.stderr.flush()
        except Exception:
            pass


_root_logger = logging.getLogger()
_root_logger.handlers.clear()
_root_logger.addHandler(_JsonStderrHandler())
_root_logger.setLevel(logging.INFO)
logger = logging.getLogger('gooya_worker')


def _worker_excepthook(exc_type, exc_value, exc_tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        return
    logger.critical('Uncaught worker crash: %s', exc_value, exc_info=(exc_type, exc_value, exc_tb))


sys.excepthook = _worker_excepthook

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
GOOYA_DIR = PROJECT_ROOT / 'Data' / 'Models' / 'gooya'
if str(GOOYA_DIR) not in sys.path:
    sys.path.insert(0, str(GOOYA_DIR))

import numpy as np
import torch
import torch.nn.functional as F

try:
    from inference import load_model
    from chatterbox.models.s3gen import S3GEN_SR
    from chatterbox.models.s3tokenizer import S3_TOKEN_RATE, SPEECH_VOCAB_SIZE, drop_invalid_tokens
    from chatterbox.models.t3.modules.cond_enc import T3Cond
    from chatterbox.mtl_tts import punc_norm
except ImportError:
    logger.exception('Failed to import Gooya / Chatterbox modules')
    load_model = None
    S3GEN_SR = 24000
    S3_TOKEN_RATE = 25
    SPEECH_VOCAB_SIZE = 6561
    drop_invalid_tokens = None
    T3Cond = None
    punc_norm = None


def _get_vram_stats() -> Dict[str, float]:
    if not torch.cuda.is_available():
        return {'vram_allocated_mb': 0.0, 'vram_reserved_mb': 0.0}
    try:
        alloc = torch.cuda.memory_allocated() / (1024.0 ** 2)
        resv = torch.cuda.memory_reserved() / (1024.0 ** 2)
        return {
            'vram_allocated_mb': round(alloc, 1),
            'vram_reserved_mb': round(resv, 1),
        }
    except Exception:
        return {'vram_allocated_mb': 0.0, 'vram_reserved_mb': 0.0}


def _normalize_persian_text(raw_text: str) -> str:
    """Clean Persian/English text for Gooya T3 without corrupting Persian punctuation."""
    text = (raw_text or '').strip()
    if not text:
        return ''
    text = text.replace('\u064a', '\u06cc').replace('\u0649', '\u06cc').replace('\u0643', '\u06a9')
    text = re.sub(r'[\U00010000-\U0010ffff]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if punc_norm is not None:
        text = punc_norm(text)
    text = re.sub(r'؟\.+$', '؟', text)
    text = re.sub(r'،\.+$', '.', text)
    return text.strip()


def _polish_chunk_audio(audio: np.ndarray, sr: int, trim_silence: bool = True) -> np.ndarray:
    """Remove DC offset, trim excessive leading/trailing dead air, and apply micro cosine fades."""
    if audio is None or audio.size == 0:
        return np.zeros(0, dtype=np.float32)

    out = np.array(audio, dtype=np.float32, copy=True)
    if out.size < int(sr * 0.05):
        return np.clip(out, -1.0, 1.0)

    mean_dc = float(np.mean(out))
    if abs(mean_dc) > 1e-4:
        out -= mean_dc

    if trim_silence:
        active = np.flatnonzero(np.abs(out) > 0.008)
        if active.size > 0:
            lead_pad = int(sr * 0.025)   # 25ms natural attack cushion
            trail_pad = int(sr * 0.075)  # 75ms natural word-ending cushion
            start = max(0, int(active[0]) - lead_pad)
            end = min(out.size, int(active[-1]) + trail_pad)
            if end - start >= int(sr * 0.08):
                out = out[start:end]

    fade_in = min(int(sr * 0.008), out.size // 4)
    fade_out = min(int(sr * 0.012), out.size // 4)
    if fade_in > 1:
        ramp_in = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, fade_in, dtype=np.float32)))
        out[:fade_in] *= ramp_in
    if fade_out > 1:
        ramp_out = 0.5 * (1.0 + np.cos(np.linspace(0.0, np.pi, fade_out, dtype=np.float32)))
        out[-fade_out:] *= ramp_out

    return np.clip(out, -1.0, 1.0)


class GooyaWorker:
    def __init__(self, model_dir: Optional[Path] = None, device: Optional[str] = None):
        self.model_dir = model_dir or GOOYA_DIR
        self.device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = None
        self.precision = 'bf16'
        self._cached_ref_path: Optional[str] = None
        self._cached_exaggeration: Optional[float] = None
        self._s3gen_supports_cfm_kwarg: bool = False
        self._flow_supports_n_timesteps: bool = False
        self._flow_n_timesteps_pos: int = 999

    def _resolve_ref_audio(self, ref_audio: Optional[str]) -> Optional[str]:
        candidates = []
        if ref_audio:
            candidates.append(Path(ref_audio))
        candidates.extend([
            self.model_dir / 'speaker_reference_clean.wav',
            self.model_dir / 'speaker_reference.wav',
            self.model_dir / 'samples' / 'canonical.wav',
        ])
        for cand in candidates:
            if cand and cand.exists():
                return str(cand)
        return None

    def _inspect_s3gen_cfm_support(self) -> None:
        self._s3gen_supports_cfm_kwarg = False
        self._flow_supports_n_timesteps = False
        self._flow_n_timesteps_pos = 999
        if self.model is None or not hasattr(self.model, 's3gen') or self.model.s3gen is None:
            return
        try:
            sig = inspect.signature(self.model.s3gen.inference)
            if 'n_cfm_timesteps' in sig.parameters:
                self._s3gen_supports_cfm_kwarg = True
                logger.info('S3Gen supports direct n_cfm_timesteps kwarg')
                return
        except Exception:
            pass

        flow = getattr(self.model.s3gen, 'flow', None)
        if flow is not None and hasattr(flow, 'inference'):
            try:
                fsig = inspect.signature(flow.inference)
                params = list(fsig.parameters.keys())
                if 'n_timesteps' in params:
                    self._flow_supports_n_timesteps = True
                    self._flow_n_timesteps_pos = params.index('n_timesteps')
                    logger.info('S3Gen flow.inference supports n_timesteps override (pos=%d)', self._flow_n_timesteps_pos)
            except Exception:
                pass

    def _run_s3gen_fp32(self, speech_tokens: torch.Tensor, cfm_timesteps: int):
        """Run S3Gen strictly in fp32 (outside autocast) with safe CFM timestep control."""
        if self._s3gen_supports_cfm_kwarg:
            return self.model.s3gen.inference(
                speech_tokens=speech_tokens,
                ref_dict=self.model.conds.gen,
                n_cfm_timesteps=cfm_timesteps,
            )

        if self._flow_supports_n_timesteps and cfm_timesteps != 10:
            flow = self.model.s3gen.flow
            orig_flow_inf = flow.inference
            pos_idx = self._flow_n_timesteps_pos

            def _patched_flow_inf(*args, **kwargs):
                if 'n_timesteps' not in kwargs and len(args) <= pos_idx:
                    kwargs['n_timesteps'] = cfm_timesteps
                return orig_flow_inf(*args, **kwargs)

            flow.inference = _patched_flow_inf
            try:
                return self.model.s3gen.inference(
                    speech_tokens=speech_tokens,
                    ref_dict=self.model.conds.gen,
                )
            finally:
                flow.inference = orig_flow_inf

        return self.model.s3gen.inference(
            speech_tokens=speech_tokens,
            ref_dict=self.model.conds.gen,
        )

    def _prepare_and_cache_conditionals(self, ref_path: str, exaggeration: float, use_bf16: bool) -> float:
        """Encode speaker reference strictly in fp32 so token IDs and mel features are never corrupted."""
        t0 = time.perf_counter()
        t3_dtype = torch.bfloat16 if use_bf16 else torch.float32

        if self._cached_ref_path != ref_path or self.model.conds is None:
            logger.info('Encoding and caching speaker reference audio (fp32): %s', ref_path)
            with torch.inference_mode():
                self.model.prepare_conditionals(ref_path, exaggeration=exaggeration)

            gen_dict = self.model.conds.gen
            for k in ('prompt_token', 'prompt_token_len'):
                if k in gen_dict and torch.is_tensor(gen_dict[k]):
                    gen_dict[k] = gen_dict[k].to(device=self.device, dtype=torch.long)
            for k in ('prompt_feat', 'embedding'):
                if k in gen_dict and torch.is_tensor(gen_dict[k]):
                    gen_dict[k] = gen_dict[k].to(device=self.device, dtype=torch.float32)

            c = self.model.conds.t3
            self.model.conds.t3 = T3Cond(
                speaker_emb=c.speaker_emb.to(device=self.device, dtype=t3_dtype),
                cond_prompt_speech_tokens=(
                    c.cond_prompt_speech_tokens.to(device=self.device, dtype=torch.long)
                    if c.cond_prompt_speech_tokens is not None else None
                ),
                emotion_adv=(exaggeration * torch.ones(1, 1, 1, device=self.device, dtype=t3_dtype)),
            )

            self._cached_ref_path = ref_path
            self._cached_exaggeration = exaggeration
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        elif self._cached_exaggeration != exaggeration:
            c = self.model.conds.t3
            self.model.conds.t3 = T3Cond(
                speaker_emb=c.speaker_emb.to(device=self.device, dtype=t3_dtype),
                cond_prompt_speech_tokens=(
                    c.cond_prompt_speech_tokens.to(device=self.device, dtype=torch.long)
                    if c.cond_prompt_speech_tokens is not None else None
                ),
                emotion_adv=(exaggeration * torch.ones(1, 1, 1, device=self.device, dtype=t3_dtype)),
            )
            self._cached_exaggeration = exaggeration

        return round((time.perf_counter() - t0) * 1000.0, 1)

    def _run_startup_warmup(self, use_bf16: bool) -> float:
        """Warm up T3 + S3Gen CUDA kernels on a tiny dummy phrase so Turn 1 has zero JIT overhead."""
        t0 = time.perf_counter()
        try:
            text_tokens = self.model.tokenizer.text_to_tokens('سلام.', language_id=None).to(self.device)
            text_tokens = torch.cat([text_tokens, text_tokens], dim=0)
            sot = self.model.t3.hp.start_text_token
            eot = self.model.t3.hp.stop_text_token
            text_tokens = F.pad(text_tokens, (1, 0), value=sot)
            text_tokens = F.pad(text_tokens, (0, 1), value=eot)

            with torch.inference_mode():
                if use_bf16:
                    with torch.autocast(device_type='cuda', dtype=torch.bfloat16):
                        st = self.model.t3.inference(
                            t3_cond=self.model.conds.t3,
                            text_tokens=text_tokens,
                            max_new_tokens=16,
                            temperature=0.75,
                            cfg_weight=0.5,
                            repetition_penalty=1.2,
                            min_p=0.05,
                            top_p=0.95,
                        )
                else:
                    st = self.model.t3.inference(
                        t3_cond=self.model.conds.t3,
                        text_tokens=text_tokens,
                        max_new_tokens=16,
                        temperature=0.75,
                        cfg_weight=0.5,
                        repetition_penalty=1.2,
                        min_p=0.05,
                        top_p=0.95,
                    )
                st = st[0]
                if drop_invalid_tokens is not None:
                    st = drop_invalid_tokens(st)
                st = st[st < SPEECH_VOCAB_SIZE].to(device=self.device, dtype=torch.long)
                if st.shape[-1] > 0:
                    wav_t, _ = self._run_s3gen_fp32(st, cfm_timesteps=4)
                    del wav_t
                del st, text_tokens

            if torch.cuda.is_available():
                torch.cuda.synchronize()
                torch.cuda.empty_cache()
        except Exception as exc:
            logger.warning('Startup CUDA warmup skipped/failed (non-fatal): %s', exc)
        return round((time.perf_counter() - t0) * 1000.0, 1)

    def load(
        self,
        model_dir: Optional[str] = None,
        precision: str = 'bf16',
        ref_audio: Optional[str] = None,
        exaggeration: float = 0.5,
        warmup: bool = True,
    ) -> dict:
        if model_dir:
            self.model_dir = Path(model_dir)
        self.precision = (precision or 'bf16').lower().strip()

        if self.model is not None:
            stats = _get_vram_stats()
            logger.info(
                'Gooya already loaded (%s) | VRAM alloc=%.1f MB, reserved=%.1f MB',
                self.precision, stats['vram_allocated_mb'], stats['vram_reserved_mb'],
            )
            return {'status': 'ok', 'message': 'already_loaded', 'pid': os.getpid(), 'load_time_s': 0.0, **stats}

        if load_model is None:
            raise RuntimeError('load_model import not available')

        # Ampere TF32 for fast fp32 S3Gen; keep cudnn.benchmark=False for variable-length audio stability & low VRAM
        if self.device == 'cuda' and torch.cuda.is_available():
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
            torch.backends.cudnn.benchmark = False
            try:
                torch.set_float32_matmul_precision('high')
            except Exception:
                pass

        t0 = time.perf_counter()
        logger.info('Loading Gooya Bozorg v1.5 from %s on %s (precision=%s, offline=1)...', self.model_dir, self.device, self.precision)
        self.model = load_model(self.model_dir, self.device)

        try:
            self.model.t3.tfmr.set_attn_implementation('sdpa')
            logger.info('Gooya T3 attention implementation set to SDPA')
        except Exception as exc:
            logger.warning('Failed to set SDPA on T3 (%s), falling back to eager', exc)
            try:
                self.model.t3.tfmr.set_attn_implementation('eager')
            except Exception as eager_exc:
                logger.warning('Failed to set eager attention on T3: %s', eager_exc)

        use_bf16 = (self.precision == 'bf16') and (self.device == 'cuda') and torch.cuda.is_available()
        if use_bf16:
            try:
                if hasattr(self.model, 't3') and self.model.t3 is not None:
                    self.model.t3.to(dtype=torch.bfloat16)
                    logger.info('Cast Gooya t3 weights to torch.bfloat16 (s3gen & ve kept in torch.float32)')
            except Exception as exc:
                logger.warning('Failed to cast t3 to bfloat16: %s', exc)

            if hasattr(self.model, 's3gen') and self.model.s3gen is not None:
                self.model.s3gen.to(dtype=torch.float32)

            gc.collect()
            torch.cuda.empty_cache()

        self._inspect_s3gen_cfm_support()
        self._cached_ref_path = None
        self._cached_exaggeration = None

        resolved_ref = self._resolve_ref_audio(ref_audio)
        if resolved_ref:
            try:
                conds_ms = self._prepare_and_cache_conditionals(resolved_ref, float(exaggeration), use_bf16)
                logger.info('Pre-cached speaker reference during load in %.1fms', conds_ms)
                if warmup and self.device == 'cuda' and torch.cuda.is_available():
                    warmup_ms = self._run_startup_warmup(use_bf16)
                    logger.info('Gooya CUDA kernel warmup done in %.1fms', warmup_ms)
            except Exception as exc:
                logger.warning('Speaker reference pre-cache/warmup failed (will retry on synth): %s', exc)

        load_time_s = round(time.perf_counter() - t0, 2)
        stats = _get_vram_stats()
        logger.info(
            'Gooya Bozorg v1.5 loaded in %.2fs (%s-hybrid) | VRAM alloc=%.1f MB, reserved=%.1f MB',
            load_time_s, self.precision, stats['vram_allocated_mb'], stats['vram_reserved_mb'],
        )
        return {'status': 'ok', 'message': 'loaded', 'pid': os.getpid(), 'load_time_s': load_time_s, **stats}

    def unload(self) -> None:
        if self.model is not None:
            logger.info('Unloading Gooya Bozorg v1.5 and releasing VRAM...')
            del self.model
            self.model = None
        self._cached_ref_path = None
        self._cached_exaggeration = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            if hasattr(torch.cuda, 'ipc_collect'):
                torch.cuda.ipc_collect()
        stats = _get_vram_stats()
        logger.info(
            'Gooya Bozorg v1.5 unloaded | VRAM alloc=%.1f MB, reserved=%.1f MB',
            stats['vram_allocated_mb'], stats['vram_reserved_mb'],
        )

    def synthesize(
        self,
        text: str,
        ref_audio: Optional[str] = None,
        exaggeration: float = 0.5,
        cfg_weight: float = 0.5,
        precision: Optional[str] = None,
        temperature: float = 0.75,
        repetition_penalty: float = 1.25,
        cfm_timesteps: int = 8,
        vram_trim_threshold_mb: float = 2450.0,
        trim_silence: bool = True,
        chunk_info: str = '',
    ) -> dict:
        if self.model is None:
            self.load(precision=precision or self.precision, ref_audio=ref_audio, exaggeration=exaggeration)

        clean_text = _normalize_persian_text(text)
        if not clean_text:
            return {'status': 'error', 'error': 'Empty text'}

        ref_path = self._resolve_ref_audio(ref_audio)
        if not ref_path:
            logger.error('Reference audio not found: %s', ref_audio)
            return {'status': 'error', 'error': f'Reference audio not found: {ref_audio}'}

        exaggeration = float(exaggeration)
        cfg_weight = float(cfg_weight)
        temperature = float(temperature)
        repetition_penalty = float(repetition_penalty)
        cfm_timesteps = max(4, min(20, int(cfm_timesteps)))
        vram_trim_threshold_mb = float(vram_trim_threshold_mb)
        use_bf16 = (self.precision == 'bf16') and (self.device == 'cuda') and torch.cuda.is_available()

        t0 = time.perf_counter()
        conds_ms = self._prepare_and_cache_conditionals(ref_path, exaggeration, use_bf16)

        text_tokens = self.model.tokenizer.text_to_tokens(clean_text, language_id=None).to(self.device)
        text_tokens = torch.cat([text_tokens, text_tokens], dim=0)
        sot = self.model.t3.hp.start_text_token
        eot = self.model.t3.hp.stop_text_token
        text_tokens = F.pad(text_tokens, (1, 0), value=sot)
        text_tokens = F.pad(text_tokens, (0, 1), value=eot)

        # Dynamic max_new_tokens cap tailored for both short chunks and full replies
        max_new_tokens = max(120, min(650, int(len(clean_text) * 3.2) + 60))

        t_t3_0 = time.perf_counter()
        with torch.inference_mode():
            if use_bf16:
                with torch.autocast(device_type='cuda', dtype=torch.bfloat16):
                    speech_tokens = self.model.t3.inference(
                        t3_cond=self.model.conds.t3,
                        text_tokens=text_tokens,
                        max_new_tokens=max_new_tokens,
                        temperature=temperature,
                        cfg_weight=cfg_weight,
                        repetition_penalty=repetition_penalty,
                        min_p=0.05,
                        top_p=0.95,
                    )
            else:
                speech_tokens = self.model.t3.inference(
                    t3_cond=self.model.conds.t3,
                    text_tokens=text_tokens,
                    max_new_tokens=max_new_tokens,
                    temperature=temperature,
                    cfg_weight=cfg_weight,
                    repetition_penalty=repetition_penalty,
                    min_p=0.05,
                    top_p=0.95,
                )

            speech_tokens = speech_tokens[0]
            if drop_invalid_tokens is not None:
                speech_tokens = drop_invalid_tokens(speech_tokens)
            speech_tokens = speech_tokens[speech_tokens < SPEECH_VOCAB_SIZE]
            speech_tokens = speech_tokens.to(device=self.device, dtype=torch.long)

        del text_tokens
        t3_ms = round((time.perf_counter() - t_t3_0) * 1000.0, 1)
        n_tokens = int(speech_tokens.shape[-1])
        if n_tokens <= 0:
            del speech_tokens
            return {'status': 'error', 'error': 'T3 generated zero valid speech tokens'}

        # S3Gen runs strictly in float32 (OUTSIDE autocast) -> zero bf16 token/length corruption
        t_s3_0 = time.perf_counter()
        with torch.inference_mode():
            wav_tensor, _ = self._run_s3gen_fp32(speech_tokens, cfm_timesteps=cfm_timesteps)
            wav = wav_tensor.squeeze(0).detach().float().cpu().numpy()

            # Drop final speech token (~40ms trailing noise before EOS)
            st_len = max(1, n_tokens - 1)
            wav = wav[: st_len * (S3GEN_SR // S3_TOKEN_RATE)]

        del wav_tensor, speech_tokens
        s3gen_ms = round((time.perf_counter() - t_s3_0) * 1000.0, 1)
        gen_ms = round(t3_ms + s3gen_ms, 1)

        sr = int(self.model.sr)
        audio_np = _polish_chunk_audio(wav, sr=sr, trim_silence=bool(trim_silence))

        stats = _get_vram_stats()
        if torch.cuda.is_available() and stats['vram_reserved_mb'] > vram_trim_threshold_mb:
            torch.cuda.empty_cache()
            stats = _get_vram_stats()

        latency_s = round(time.perf_counter() - t0, 3)
        audio_duration_s = round(len(audio_np) / float(sr), 3) if sr > 0 else 0.0
        rtf = round(latency_s / max(audio_duration_s, 1e-6), 3)
        chunk_tag = f' [chunk {chunk_info}]' if chunk_info else ''

        logger.info(
            'Synthesized%s %.2fs audio in %.2fs | chars=%d tokens=%d/%d cfm=%d (conds=%.1fms t3=%.1fms s3gen=%.1fms RTF=%.2f) | VRAM alloc=%.1f MB, reserved=%.1f MB',
            chunk_tag, audio_duration_s, latency_s, len(clean_text), n_tokens, max_new_tokens,
            cfm_timesteps, conds_ms, t3_ms, s3gen_ms, rtf,
            stats['vram_allocated_mb'], stats['vram_reserved_mb'],
        )

        b64 = base64.b64encode(audio_np.tobytes()).decode('ascii')
        return {
            'status': 'ok',
            'sample_rate': sr,
            'audio_b64': b64,
            'num_samples': int(len(audio_np)),
            'speech_tokens': n_tokens,
            'cfm_timesteps': cfm_timesteps,
            'latency_s': latency_s,
            'conds_ms': conds_ms,
            't3_ms': t3_ms,
            's3gen_ms': s3gen_ms,
            'gen_ms': gen_ms,
            'audio_duration_s': audio_duration_s,
            'rtf': rtf,
            **stats,
        }


def main():
    global _current_turn_id
    worker = GooyaWorker()
    real_pid = os.getpid()
    logger.info('GooyaWorker started (PID=%d, device=%s, offline=1)', real_pid, worker.device)
    sys.stdout.write(RESP_PREFIX + json.dumps({'status': 'ready', 'pid': real_pid}) + '\n')
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:
            logger.error('Invalid JSON request received: %s', exc)
            sys.stdout.write(RESP_PREFIX + json.dumps({'status': 'error', 'error': f'Invalid JSON: {exc}'}) + '\n')
            sys.stdout.flush()
            continue

        _current_turn_id = str(req.get('turn_id') or '-').strip() or '-'
        cmd = req.get('command', 'synthesize')
        try:
            if cmd == 'ping':
                resp = {'status': 'ok', 'message': 'pong', 'pid': real_pid, **_get_vram_stats()}
            elif cmd == 'load':
                resp = worker.load(
                    model_dir=req.get('model_dir'),
                    precision=req.get('precision', 'bf16'),
                    ref_audio=req.get('ref_audio'),
                    exaggeration=req.get('exaggeration', 0.5),
                    warmup=bool(req.get('warmup', True)),
                )
            elif cmd == 'synthesize':
                resp = worker.synthesize(
                    text=req.get('text', ''),
                    ref_audio=req.get('ref_audio'),
                    exaggeration=req.get('exaggeration', 0.5),
                    cfg_weight=req.get('cfg_weight', 0.5),
                    precision=req.get('precision'),
                    temperature=req.get('temperature', 0.75),
                    repetition_penalty=req.get('repetition_penalty', 1.25),
                    cfm_timesteps=int(req.get('cfm_timesteps', 8)),
                    vram_trim_threshold_mb=float(req.get('vram_trim_threshold_mb', 2450.0)),
                    trim_silence=bool(req.get('trim_silence', True)),
                    chunk_info=str(req.get('chunk_info', '')),
                )
            elif cmd == 'unload':
                worker.unload()
                resp = {'status': 'ok', 'message': 'unloaded', 'pid': real_pid, **_get_vram_stats()}
            elif cmd == 'exit':
                worker.unload()
                resp = {'status': 'ok', 'message': 'exiting', 'pid': real_pid}
                sys.stdout.write(RESP_PREFIX + json.dumps(resp) + '\n')
                sys.stdout.flush()
                break
            else:
                resp = {'status': 'error', 'error': f'Unknown command: {cmd}'}
        except Exception as exc:
            logger.exception('Worker command "%s" error: %s', cmd, exc)
            resp = {'status': 'error', 'error': str(exc)}
        finally:
            _current_turn_id = '-'

        sys.stdout.write(RESP_PREFIX + json.dumps(resp) + '\n')
        sys.stdout.flush()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())