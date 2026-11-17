"""C.E.L.E.S.T.E. Voice - Dual Engine TTS (XTTS Local CPU + Gemini Online)"""
import os
import io
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
    """
    Dual TTS Engine Architecture:
    1. Online: Google Gemini Native Audio (Emotion Director)
    2. Local: ParsVoice-XTTSv2 (running exclusively on CPU to save VRAM)
    """

    DEFAULT_SAMPLE_RATE = 24000
    SPEAK_QUEUE_MAX = 15

    def __init__(self):
        self._xtts_model = None
        self._xtts_available = False
        self._initialized = False
        self._speaking = False
        self._speech_generation_id = 0
        self._lock = threading.Lock()
        self._play_lock = threading.Lock()
        self._queue = []
        self.initialize()

    def _load_config(self) -> Dict[str, Any]:
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                return data.get("voice", {}) or {}
        except Exception:
            pass
        return {}

    def initialize(self) -> bool:
        if self._initialized:
            return True
        
        cfg = self._load_config()
        # Initialize Local XTTS Model on CPU
        try:
            import torch
            from TTS.api import TTS
            
            xtts_path = PROJECT_ROOT / cfg.get("xtts_model_path", "Data/Models/xtts/ParsVoice")
            
            if xtts_path.exists() or cfg.get("local_engine") == "xtts":
                # Strict CPU enforcement to avoid OOM with LLM
                logger.info("Initializing ParsVoice-XTTS on CPU...")
                # We use standard TTS API but you can point it to local path if downloaded
                self._xtts_model = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to("cpu")
                self._xtts_available = True
                logger.info("XTTS Model Loaded Successfully on CPU.")
        except ImportError:
            logger.warning("Coqui TTS not installed. Run: pip install TTS")
        except Exception as e:
            logger.error("Failed to load XTTS: %s", e)

        self._initialized = True
        return True

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

        text = re.sub(r"[*`#_~>|•]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        
        return explicit_tone or "صمیمی و طبیعی", text

    def speak(self, text: str, blocking: bool = False) -> bool:
        if not self._initialized:
            self.initialize()

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
            t = threading.Thread(target=self._speak_worker, args=(text, gen_id), daemon=True)
            t.start()
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
                mode = cfg.get("engine_mode", "online")
                tone, clean_text = self.clean_text_for_tts(raw_text)

                pcm = None
                sr = self.DEFAULT_SAMPLE_RATE

                if mode == "online":
                    pcm, sr = self._synthesize_gemini(clean_text, tone, cfg)
                
                # Fallback to local if online failed or mode is local
                if (pcm is None or len(pcm) == 0) and self._xtts_available:
                    pcm, sr = self._synthesize_xtts(clean_text, cfg)

                if pcm is not None and len(pcm) > 0 and gen_id == self._speech_generation_id:
                    self._playback_audio(pcm, sample_rate=sr, blocking=False)
                    return True

                return False
            except Exception as exc:
                logger.error("TTS worker failed: %s", exc)
                return False
            finally:
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
                        # Read WAV container
                        with wave.open(io.BytesIO(raw_audio), "rb") as wf:
                            sr = wf.getframerate()
                            frames = wf.readframes(wf.getnframes())
                            pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                        return self._apply_dsp(pcm, sr), sr
        except Exception as e:
            logger.warning("Gemini TTS Error: %s", e)
        return None, self.DEFAULT_SAMPLE_RATE

    def _synthesize_xtts(self, text: str, cfg: dict) -> Tuple[Optional[np.ndarray], int]:
        if not self._xtts_model:
            return None, 24000
            
        speaker_wav = PROJECT_ROOT / cfg.get("xtts_speaker_wav", "Data/Models/xtts/speaker_reference.wav")
        if not speaker_wav.exists():
            logger.error("XTTS requires a speaker_reference.wav file!")
            return None, 24000

        try:
            # Generate audio matrix directly (no file write)
            wav_output = self._xtts_model.tts(text=text, speaker_wav=str(speaker_wav), language="fa")
            pcm = np.array(wav_output, dtype=np.float32)
            sr = 24000  # XTTS standard
            return self._apply_dsp(pcm, sr), sr
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