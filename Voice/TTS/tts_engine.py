"""C.E.L.E.S.T.E. Voice - Tri-Engine TTS with Dynamic Resource Allocation"""
import os
import io
import gc
import json
import wave
import base64
import logging
import threading
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "Configuration" / "config.json"
logger = logging.getLogger(__name__)

class TTSEngine:
    DEFAULT_SAMPLE_RATE = 24000
    SPEAK_QUEUE_MAX = 15

    def __init__(self, model_name: Optional[str] = None, *args, **kwargs):
        self._pocket_model = None
        self._pocket_voice_state = None
        self._xtts_model = None
        self._initialized = False
        self._speaking = False
        self._speech_generation_id = 0
        self._lock = threading.Lock()
        self._play_lock = threading.Lock()
        self._queue = []
        self._load_config()
        self._initialized = True

    def initialize(self) -> bool:
        self._load_config()
        self._initialized = True
        return True

    def get_status(self) -> Dict[str, Any]:
        cfg = self._load_config()
        return {
            "initialized": self._initialized,
            "speaking": self._speaking,
            "engine_mode": cfg.get("engine_mode", "online"),
            "local_engine": cfg.get("local_engine", "pocket"),
            "pocket_loaded": self._pocket_model is not None,
            "xtts_loaded": self._xtts_model is not None,
        }

    def _load_config(self) -> Dict[str, Any]:
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                return data.get("voice", {}) or {}
        except Exception:
            pass
        return {}

    def _force_garbage_collect(self):
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                if hasattr(torch.cuda, "ipc_collect"):
                    torch.cuda.ipc_collect()
                logger.info("[Memory Manager] CUDA cache cleared. VRAM freed.")
        except Exception:
            pass

    def _unload_pocket(self):
        if self._pocket_voice_state is not None:
            del self._pocket_voice_state
            self._pocket_voice_state = None
        if self._pocket_model is not None:
            logger.info("[Memory Manager] Unloading Pocket-TTS from memory...")
            del self._pocket_model
            self._pocket_model = None
            self._force_garbage_collect()

    def _unload_xtts(self):
        if self._xtts_model is not None:
            logger.info("[Memory Manager] Unloading ParsVoice-XTTS from memory...")
            del self._xtts_model
            self._xtts_model = None
            self._force_garbage_collect()

    def force_sync_memory_state(self, engine_mode: str, local_engine: str):
        # Wrap memory unload in _play_lock to prevent racing (Flash Fix 3)
        with self._play_lock:
            with self._lock:
                if engine_mode == "online":
                    self._unload_pocket()
                    self._unload_xtts()
                elif engine_mode == "local":
                    if local_engine == "pocket":
                        self._unload_xtts()
                    elif local_engine == "xtts":
                        self._unload_pocket()

    def _get_pocket_model(self, cfg: dict):
        if self._pocket_model is None:
            import torch
            from pocket_tts import TTSModel
            
            device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info(f"[Loader] Loading Pocket-TTS Farsi v2 on {device.upper()}...")
            
            self._pocket_model = TTSModel.load_model(config="hf://mehdi-hf/pocket-tts-farsi-v2/farsi.yaml", temp=0.3)
            self._pocket_model.to(device)
            
            speaker_wav = PROJECT_ROOT / cfg.get("xtts_speaker_wav", "Data/Models/pocket/speaker_reference.wav")
            if speaker_wav.exists():
                self._pocket_voice_state = self._pocket_model.get_state_for_audio_prompt(str(speaker_wav))
                if hasattr(self._pocket_voice_state, "to"):
                    self._pocket_voice_state = self._pocket_voice_state.to(device)
            else:
                self._pocket_voice_state = None
        return self._pocket_model, self._pocket_voice_state

    def _get_xtts_model(self, cfg: dict):
        if self._xtts_model is None:
            from TTS.api import TTS
            logger.info("[Loader] Loading ParsVoice-XTTS on CPU...")
            self._xtts_model = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to("cpu")
        return self._xtts_model

    @staticmethod
    def clean_text_for_tts(raw_text: str) -> Tuple[str, str]:
        if not raw_text:
            return "طبیعی", ""
        text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL)
        text = re.sub(r"<action>.*?</action>", "", text, flags=re.DOTALL)
        text = re.sub(r"<fact>.*?</fact>", "", text, flags=re.DOTALL)
        text = re.sub(r"\[Action.*?\]", "", text)
        text = re.sub(r"C\s*\.\s*E\s*\.\s*L\s*\.\s*E\s*\.\s*S\s*\.\s*T\s*\.\s*E\.?", "سلست", text, flags=re.IGNORECASE)
        tone_match = re.search(r"\[\s*(?:لحن|حس|tone)\s*:\s*([^\]]+)\]", text, flags=re.IGNORECASE)
        explicit_tone = ""
        if tone_match:
            explicit_tone = tone_match.group(1).strip()
            text = text[:tone_match.start()] + text[tone_match.end():]
        text = re.sub(r"[*#_~>|•]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return explicit_tone or "صمیمی و طبیعی", text

    def speak(self, text: str, blocking: bool = False) -> bool:
        _, clean_text = self.clean_text_for_tts(text)
        if not clean_text:
            return False
        with self._lock:
            self._speech_generation_id += 1
            gen_id = self._speech_generation_id
            self._queue.append(text)
            if len(self._queue) > self.SPEAK_QUEUE_MAX:
                self._queue.pop(0)
        if blocking:
            return self._speak_worker(text, gen_id)
        else:
            threading.Thread(target=self._speak_worker, args=(text, gen_id), daemon=True).start()
            return True

    def stop_speaking(self) -> None:
        with self._lock:
            self._speech_generation_id += 1
            self._speaking = False
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def _speak_worker(self, raw_text: str, gen_id: int) -> bool:
        with self._play_lock:
            with self._lock:
                if gen_id != self._speech_generation_id:
                    return False
                self._speaking = True

            try:
                cfg = self._load_config()
                engine_mode = cfg.get("engine_mode", "online")
                local_engine = cfg.get("local_engine", "pocket")
                tone, clean_text = self.clean_text_for_tts(raw_text)

                pcm = None
                sr = self.DEFAULT_SAMPLE_RATE

                if engine_mode == "online":
                    pcm, sr = self._synthesize_gemini(clean_text, tone, cfg)
                
                if pcm is None or len(pcm) == 0 or engine_mode == "local":
                    if local_engine == "xtts":
                        pcm, sr = self._synthesize_xtts(clean_text, cfg)
                    else:
                        pcm, sr = self._synthesize_pocket(clean_text, cfg)

                if pcm is not None and len(pcm) > 0 and gen_id == self._speech_generation_id:
                    self._playback_audio(pcm, sample_rate=sr, blocking=True)
                    return True

                return False
            except Exception as exc:
                logger.error("TTS worker failed: %s", exc)
                return False
            finally:
                if engine_mode == "online" and (self._pocket_model is not None or self._xtts_model is not None):
                    self._unload_pocket()
                    self._unload_xtts()
                with self._lock:
                    if gen_id == self._speech_generation_id:
                        self._speaking = False

    def _synthesize_gemini(self, text: str, tone: str, cfg: dict) -> Tuple[Optional[np.ndarray], int]:
        api_key = cfg.get("online_api_key")
        if not api_key:
            return None, self.DEFAULT_SAMPLE_RATE
        prompt = (f"متن زیر را با لهجه اصیل و طبیعیِ فارسیِ تهرانی، با حس و لحنِ «{tone}» بخوان:\n\n«{text}»")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent?key={api_key}"
        try:
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": cfg.get("online_voice_name", "Kore")}}}
                }
            }
            resp = requests.post(url, json=payload, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                parts = data.get("candidates", [])[0].get("content", {}).get("parts", [])
                for part in parts:
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        raw_audio = base64.b64decode(inline["data"])
                        # Flash Fix 5: Handle both RIFF headers and raw 24kHz 16-bit PCM payloads
                        if raw_audio.startswith(b"RIFF"):
                            with wave.open(io.BytesIO(raw_audio), "rb") as wf:
                                sr = wf.getframerate()
                                frames = wf.readframes(wf.getnframes())
                                pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                        else:
                            sr = 24000
                            if len(raw_audio) % 2 != 0:
                                raw_audio = raw_audio[:-1]
                            pcm = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32) / 32768.0
                        return self._apply_dsp(pcm, sr), sr
        except Exception as e:
            logger.warning("Gemini TTS Error: %s", e)
        return None, self.DEFAULT_SAMPLE_RATE

    def _synthesize_pocket(self, text: str, cfg: dict) -> Tuple[Optional[np.ndarray], int]:
        try:
            model, v_state = self._get_pocket_model(cfg)
            if v_state is None and hasattr(model, "get_default_state"):
                v_state = model.get_default_state()
            audio_tensor = model.generate_audio(v_state, text, frames_after_eos=0)
            pcm = audio_tensor.detach().cpu().numpy().astype(np.float32)
            sr = model.sample_rate
            return self._apply_dsp(pcm, sr), sr
        except Exception as e:
            logger.error("Pocket-TTS Synthesis Error: %s", e)
            return None, self.DEFAULT_SAMPLE_RATE

    def _synthesize_xtts(self, text: str, cfg: dict) -> Tuple[Optional[np.ndarray], int]:
        try:
            model = self._get_xtts_model(cfg)
            speaker_wav = PROJECT_ROOT / cfg.get("xtts_speaker_wav", "Data/Models/xtts/speaker_reference.wav")
            if not speaker_wav.exists():
                logger.error(f"XTTS Reference audio not found at {speaker_wav}")
                return None, 24000
            wav_output = model.tts(text=text, speaker_wav=str(speaker_wav), language="fa")
            pcm = np.array(wav_output, dtype=np.float32)
            return self._apply_dsp(pcm, 24000), 24000
        except Exception as e:
            logger.error("XTTS Synthesis Error: %s", e)
            return None, 24000

    def _apply_dsp(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        try:
            from Voice.AudioDSP.jarvis_dsp import JarvisAudioDSP
            if getattr(self, "_dsp", None) is None or self._dsp._sr != sample_rate:
                self._dsp = JarvisAudioDSP(sample_rate=sample_rate)
            return self._dsp.process(pcm)
        except Exception:
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
                with wave.open(buf, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    wf.writeframes(pcm_int16.tobytes())
                winsound.PlaySound(buf.getvalue(), winsound.SND_MEMORY)
            except Exception as exc:
                logger.error("Playback error: %s", exc)
        if blocking:
            _play()
        else:
            threading.Thread(target=_play, daemon=True).start()
