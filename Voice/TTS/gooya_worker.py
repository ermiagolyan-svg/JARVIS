# C.E.L.E.S.T.E. Voice - Gooya Bozorg v1.5 Isolated Low-Latency Worker
# Runs in Data/Models/gooya/venv (Python 3.11 with torch 2.6.0+cu124)
from __future__ import annotations

import base64
import gc
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Dict, Optional

# Ignore noisy warnings and force line-buffered UTF-8 stderr/stdout
os.environ['PYTHONWARNINGS'] = 'ignore'
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

try:
    from inference import load_model
except ImportError:
    logger.exception('Failed to import load_model from Data/Models/gooya/inference.py')
    load_model = None


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


class GooyaWorker:
    def __init__(self, model_dir: Optional[Path] = None, device: Optional[str] = None):
        self.model_dir = model_dir or GOOYA_DIR
        self.device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = None
        self.precision = 'bf16'
        self._cached_ref_path: Optional[str] = None
        self._cached_exaggeration: Optional[float] = None

    def load(self, model_dir: Optional[str] = None, precision: str = 'bf16') -> dict:
        if model_dir:
            self.model_dir = Path(model_dir)
        self.precision = (precision or 'bf16').lower().strip()

        if self.model is not None:
            stats = _get_vram_stats()
            logger.info(
                'Gooya already loaded (%s) | VRAM alloc=%.1f MB, reserved=%.1f MB',
                self.precision, stats['vram_allocated_mb'], stats['vram_reserved_mb'],
            )
            return {'status': 'ok', 'message': 'already_loaded', 'load_time_s': 0.0, **stats}

        if load_model is None:
            raise RuntimeError('load_model import not available')

        t0 = time.perf_counter()
        logger.info('Loading Gooya Bozorg v1.5 from %s on %s (precision=%s)...', self.model_dir, self.device, self.precision)
        self.model = load_model(self.model_dir, self.device)

        # 1. Enable PyTorch SDPA FlashAttention on T3 with fallback to eager
        try:
            self.model.t3.tfmr.set_attn_implementation('sdpa')
            logger.info('Gooya T3 attention implementation set to SDPA (FlashAttention)')
        except Exception as exc:
            logger.warning('Failed to set SDPA on T3 (%s), falling back to eager', exc)
            try:
                self.model.t3.tfmr.set_attn_implementation('eager')
            except Exception as eager_exc:
                logger.warning('Failed to set eager attention on T3: %s', eager_exc)

        # 2. Cast heavy modules (t3 and s3gen.flow) to bfloat16 on CUDA (~1.6 GB vs ~3.1 GB fp32)
        if self.precision == 'bf16' and self.device == 'cuda' and torch.cuda.is_available():
            try:
                if hasattr(self.model, 't3') and self.model.t3 is not None:
                    self.model.t3.to(dtype=torch.bfloat16)
                    logger.info('Cast Gooya t3 weights to torch.bfloat16')
            except Exception as exc:
                logger.warning('Failed to cast t3 to bfloat16: %s', exc)

            try:
                if hasattr(self.model, 's3gen') and hasattr(self.model.s3gen, 'flow') and self.model.s3gen.flow is not None:
                    self.model.s3gen.flow.to(dtype=torch.bfloat16)
                    logger.info('Cast Gooya s3gen.flow weights to torch.bfloat16')
            except Exception as exc:
                logger.warning('Failed to cast s3gen.flow to bfloat16: %s', exc)

            gc.collect()
            torch.cuda.empty_cache()

        self._cached_ref_path = None
        self._cached_exaggeration = None

        load_time_s = round(time.perf_counter() - t0, 2)
        stats = _get_vram_stats()
        logger.info(
            'Gooya Bozorg v1.5 loaded in %.2fs (%s) | VRAM alloc=%.1f MB, reserved=%.1f MB',
            load_time_s, self.precision, stats['vram_allocated_mb'], stats['vram_reserved_mb'],
        )
        return {'status': 'ok', 'message': 'loaded', 'load_time_s': load_time_s, **stats}

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
    ) -> dict:
        if self.model is None:
            self.load(precision=precision or self.precision)

        if not text or not text.strip():
            return {'status': 'error', 'error': 'Empty text'}

        ref_path = ref_audio or str(self.model_dir / 'speaker_reference.wav')
        if not Path(ref_path).exists():
            cand = self.model_dir / 'samples' / 'canonical.wav'
            if cand.exists():
                ref_path = str(cand)
            else:
                logger.error('Reference audio not found: %s', ref_path)
                return {'status': 'error', 'error': f'Reference audio not found: {ref_path}'}

        exaggeration = float(exaggeration)
        cfg_weight = float(cfg_weight)
        use_bf16 = (self.precision == 'bf16') and (self.device == 'cuda') and torch.cuda.is_available()

        t0 = time.perf_counter()
        t_conds0 = t0

        # Speaker Embedding Cache: prepare conditionals only when ref audio changes
        if self._cached_ref_path != ref_path or self.model.conds is None:
            logger.info('Encoding and caching speaker reference audio: %s', ref_path)
            with torch.inference_mode():
                if use_bf16:
                    with torch.autocast(device_type='cuda', dtype=torch.bfloat16):
                        self.model.prepare_conditionals(ref_path, exaggeration=exaggeration)
                else:
                    self.model.prepare_conditionals(ref_path, exaggeration=exaggeration)
            self._cached_ref_path = ref_path
            self._cached_exaggeration = exaggeration
        elif self._cached_exaggeration != exaggeration:
            if hasattr(self.model.conds, 't3') and hasattr(self.model.conds.t3, 'emotion_adv'):
                from chatterbox.models.t3.modules.cond_enc import T3Cond
                c = self.model.conds.t3
                emb_dtype = c.speaker_emb.dtype if hasattr(c, 'speaker_emb') and c.speaker_emb is not None else torch.float32
                self.model.conds.t3 = T3Cond(
                    speaker_emb=c.speaker_emb,
                    cond_prompt_speech_tokens=c.cond_prompt_speech_tokens,
                    emotion_adv=(exaggeration * torch.ones(1, 1, 1, dtype=emb_dtype)),
                ).to(device=self.device)
                self._cached_exaggeration = exaggeration

        conds_ms = round((time.perf_counter() - t_conds0) * 1000.0, 1)
        t_gen0 = time.perf_counter()

        with torch.inference_mode():
            if use_bf16:
                with torch.autocast(device_type='cuda', dtype=torch.bfloat16):
                    wav = self.model.generate(
                        text=text.strip(),
                        language_id=None,
                        audio_prompt_path=None,
                        exaggeration=exaggeration,
                        cfg_weight=cfg_weight,
                    )
            else:
                wav = self.model.generate(
                    text=text.strip(),
                    language_id=None,
                    audio_prompt_path=None,
                    exaggeration=exaggeration,
                    cfg_weight=cfg_weight,
                )

        gen_ms = round((time.perf_counter() - t_gen0) * 1000.0, 1)
        audio_np = wav.squeeze().float().cpu().numpy().astype(np.float32)
        sr = int(self.model.sr)
        latency_s = round(time.perf_counter() - t0, 3)
        audio_duration_s = round(len(audio_np) / float(sr), 3) if sr > 0 else 0.0
        rtf = round(latency_s / max(audio_duration_s, 1e-6), 3)
        stats = _get_vram_stats()

        logger.info(
            'Synthesized %.2fs audio in %.2fs (conds=%.1fms gen=%.1fms RTF=%.2f) | VRAM alloc=%.1f MB, reserved=%.1f MB',
            audio_duration_s, latency_s, conds_ms, gen_ms, rtf,
            stats['vram_allocated_mb'], stats['vram_reserved_mb'],
        )

        b64 = base64.b64encode(audio_np.tobytes()).decode('ascii')
        return {
            'status': 'ok',
            'sample_rate': sr,
            'audio_b64': b64,
            'num_samples': int(len(audio_np)),
            'latency_s': latency_s,
            'conds_ms': conds_ms,
            'gen_ms': gen_ms,
            'audio_duration_s': audio_duration_s,
            'rtf': rtf,
            **stats,
        }


def main():
    global _current_turn_id
    worker = GooyaWorker()
    logger.info('GooyaWorker started (PID=%d, device=%s)', os.getpid(), worker.device)
    sys.stdout.write(RESP_PREFIX + json.dumps({'status': 'ready'}) + '\n')
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
                resp = {'status': 'ok', 'message': 'pong', **_get_vram_stats()}
            elif cmd == 'load':
                resp = worker.load(model_dir=req.get('model_dir'), precision=req.get('precision', 'bf16'))
            elif cmd == 'synthesize':
                resp = worker.synthesize(
                    text=req.get('text', ''),
                    ref_audio=req.get('ref_audio'),
                    exaggeration=req.get('exaggeration', 0.5),
                    cfg_weight=req.get('cfg_weight', 0.5),
                    precision=req.get('precision'),
                )
            elif cmd == 'unload':
                worker.unload()
                resp = {'status': 'ok', 'message': 'unloaded', **_get_vram_stats()}
            elif cmd == 'exit':
                worker.unload()
                resp = {'status': 'ok', 'message': 'exiting'}
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