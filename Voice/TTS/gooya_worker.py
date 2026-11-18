# C.E.L.E.S.T.E. Voice - Gooya Bozorg v1.5 Isolated Low-Latency Worker
# Runs in Data/Models/gooya/venv (Python 3.11 with torch 2.6.0+cu124)
from __future__ import annotations

import base64
import gc
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Ignore warnings and set unbuffered standard streams
os.environ['PYTHONWARNINGS'] = 'ignore'
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(levelname)s: [GooyaWorker] %(message)s')
logger = logging.getLogger('gooya_worker')

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
GOOYA_DIR = PROJECT_ROOT / 'Data' / 'Models' / 'gooya'
if str(GOOYA_DIR) not in sys.path:
    sys.path.insert(0, str(GOOYA_DIR))

import numpy as np
import torch

try:
    from inference import load_model
except ImportError:
    logger.error('Failed to import load_model from Data/Models/gooya/inference.py')
    load_model = None

RESP_PREFIX = 'GOOYA_WORKER_RESP:'


class GooyaWorker:
    def __init__(self, model_dir: Optional[Path] = None, device: Optional[str] = None):
        self.model_dir = model_dir or GOOYA_DIR
        self.device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = None
        self.precision = 'bf16'
        self._cached_ref_path: Optional[str] = None
        self._cached_exaggeration: Optional[float] = None

    def load(self, model_dir: Optional[str] = None, precision: str = 'bf16') -> bool:
        if model_dir:
            self.model_dir = Path(model_dir)
        self.precision = precision
        if self.model is not None:
            return True

        if load_model is None:
            raise RuntimeError('load_model import not available')

        logger.info('Loading Gooya Bozorg v1.5 from %s on %s...', self.model_dir, self.device)
        self.model = load_model(self.model_dir, self.device)

        # 1. Enable PyTorch SDPA FlashAttention on T3 with fallback to eager
        try:
            self.model.t3.tfmr.set_attn_implementation('sdpa')
            logger.info('Gooya T3 attention implementation set to SDPA (FlashAttention)')
        except Exception as exc:
            logger.warning('Failed to set SDPA on T3 (%s), falling back to eager', exc)
            try:
                self.model.t3.tfmr.set_attn_implementation('eager')
            except Exception:
                pass

        self._cached_ref_path = None
        self._cached_exaggeration = None
        logger.info('Gooya Bozorg v1.5 loaded successfully')
        return True

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
        logger.info('Gooya Bozorg v1.5 unloaded, VRAM released.')

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
            # Fallback to samples/canonical.wav
            cand = self.model_dir / 'samples' / 'canonical.wav'
            if cand.exists():
                ref_path = str(cand)
            else:
                return {'status': 'error', 'error': f'Reference audio not found: {ref_path}'}

        exaggeration = float(exaggeration)
        cfg_weight = float(cfg_weight)

        # Speaker Embedding Cache: prepare conditionals only when ref audio changes
        if self._cached_ref_path != ref_path or self.model.conds is None:
            logger.info('Encoding and caching speaker reference audio: %s', ref_path)
            self.model.prepare_conditionals(ref_path, exaggeration=exaggeration)
            self._cached_ref_path = ref_path
            self._cached_exaggeration = exaggeration
        elif self._cached_exaggeration != exaggeration:
            if hasattr(self.model.conds, 't3') and hasattr(self.model.conds.t3, 'emotion_adv'):
                from chatterbox.models.t3.modules.cond_enc import T3Cond
                c = self.model.conds.t3
                self.model.conds.t3 = T3Cond(
                    speaker_emb=c.speaker_emb,
                    cond_prompt_speech_tokens=c.cond_prompt_speech_tokens,
                    emotion_adv=exaggeration * torch.ones(1, 1, 1),
                ).to(device=self.device)
                self._cached_exaggeration = exaggeration

        use_bf16 = (self.precision == 'bf16') and (self.device == 'cuda') and torch.cuda.is_available()

        if use_bf16:
            with torch.autocast(device_type='cuda', dtype=torch.bfloat16):
                wav = self.model.generate(
                    text=text.strip(),
                    language_id=None,
                    audio_prompt_path=None,  # Use cached conditionals
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

        audio_np = wav.squeeze().cpu().numpy().astype(np.float32)
        b64 = base64.b64encode(audio_np.tobytes()).decode('ascii')
        return {
            'status': 'ok',
            'sample_rate': int(self.model.sr),
            'audio_b64': b64,
            'num_samples': int(len(audio_np)),
        }


def main():
    worker = GooyaWorker()
    # Signal ready
    sys.stdout.write(RESP_PREFIX + json.dumps({'status': 'ready'}) + '\n')
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:
            sys.stdout.write(RESP_PREFIX + json.dumps({'status': 'error', 'error': f'Invalid JSON: {exc}'}) + '\n')
            sys.stdout.flush()
            continue

        cmd = req.get('command', 'synthesize')
        try:
            if cmd == 'ping':
                resp = {'status': 'ok', 'message': 'pong'}
            elif cmd == 'load':
                worker.load(model_dir=req.get('model_dir'), precision=req.get('precision', 'bf16'))
                resp = {'status': 'ok', 'message': 'loaded'}
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
                resp = {'status': 'ok', 'message': 'unloaded'}
            elif cmd == 'exit':
                worker.unload()
                resp = {'status': 'ok', 'message': 'exiting'}
                sys.stdout.write(RESP_PREFIX + json.dumps(resp) + '\n')
                sys.stdout.flush()
                break
            else:
                resp = {'status': 'error', 'error': f'Unknown command: {cmd}'}
        except Exception as exc:
            logger.error('Worker command error: %s', exc)
            resp = {'status': 'error', 'error': str(exc)}

        sys.stdout.write(RESP_PREFIX + json.dumps(resp) + '\n')
        sys.stdout.flush()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
