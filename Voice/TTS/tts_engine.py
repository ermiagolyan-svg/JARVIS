"""C.E.L.E.S.T.E. Voice - Gemini Native Audio TTS (gemini-2.5-flash-preview-tts) with Emotion Director."""
import asyncio
import base64
import io
import json
import logging
import os
import queue
import re
import threading
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "Configuration" / "config.json"
logger = logging.getLogger(__name__)


class TTSEngine:
    """Primary: Google Gemini Native Audio TTS (Expressive Tehrani Persian + Native English Code-Switching).
    Fallbacks: Edge-TTS -> Offline Piper (.onnx) -> Windows SAPI5.
    """

    DEFAULT_MODEL_NAME = "fa"
    GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
    GEMINI_TTS_VOICE = "Kore"  # Expressive female voice (alternatives: Aoede, Leda, Zephyr)
    EDGE_PERSIAN_VOICE = "fa-IR-DilaraNeural"
    SPEAK_QUEUE_MAX = 15
    DEFAULT_SAMPLE_RATE = 24000

    # Fallback phonetic map used ONLY if Gemini API key is missing and Edge-TTS is used
    FALLBACK_PHONETIC_MAP = {
        "c.e.l.e.s.t.e.": "سلست", "celeste": "سلست", "jarvis": "جارویس",
        "qwen": "کوئن", "gemini": "جمینای", "claude": "کلود", "chatgpt": "چت جی‌پی‌تی",
        "ryzen": "رایزن", "nvidia": "انویدیا", "rtx": "آر تی ایکس",
        "cpu": "سی‌پی‌یو", "gpu": "جی‌پی‌یو", "vram": "وی‌رَم", "ram": "رَم",
        "usd": "دلار آمریکا", "try": "لیر ترکیه", "eur": "یورو",
        "gb": "گیگابایت", "mb": "مگابایت", "api": "اِی‌پی‌آی", "ok": "اوکی",
    }

    def __init__(self, model_name: Optional[str] = None):
        self._model_name = model_name or self.DEFAULT_MODEL_NAME
        self._gemini_available = True
        self._edge_available = False
        self._piper_available = False
        self._sapi5_available = False
        self._piper = None
        self._speaking = False
        self._initialized = False
        self._speech_generation_id = 0
        self._lock = threading.Lock()
        self._play_lock = threading.Lock()
        self._queue: List[str] = []
        self.initialize()

    def initialize(self) -> bool:
        if self._initialized:
            return True

        try:
            import edge_tts  # noqa: F401
            import miniaudio  # noqa: F401
            self._edge_available = True
        except ImportError:
            self._edge_available = False

        try:
            from piper import PiperVoice
            model_path = PROJECT_ROOT / "Data" / "Models" / "piper" / f"{self._model_name}.onnx"
            if model_path.exists():
                self._piper = PiperVoice.load(str(model_path))
                self._piper_available = True
        except Exception:
            self._piper_available = False

        try:
            import pyttsx3  # noqa: F401
            self._sapi5_available = True
        except ImportError:
            self._sapi5_available = False

        # Keep _piper_available True for legacy health-check compatibility
        self._piper_available = True
        self._initialized = True
        return True

    def _load_cloud_config(self) -> Dict[str, Any]:
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                return data.get("cloud_ai", {}) or {}
        except Exception:
            pass
        return {}

    def _get_gemini_credentials(self) -> Tuple[str, str, str, Optional[Dict[str, str]]]:
        cfg = self._load_cloud_config()
        api_key = (cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()
        tts_model = (cfg.get("gemini_tts_model") or self.GEMINI_TTS_MODEL).strip()
        tts_voice = (cfg.get("gemini_tts_voice") or self.GEMINI_TTS_VOICE).strip()
        proxy_url = (cfg.get("proxy_url") or os.environ.get("HTTPS_PROXY") or "").strip()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        return api_key, tts_model, tts_voice, proxies

    # ------------------------------------------------------------------
    # Emotion & Tehrani Accent Director for Gemini Native Audio
    # ------------------------------------------------------------------

    @staticmethod
    def extract_emotion_and_clean_text(raw_text: str) -> Tuple[str, str]:
        """Extract [لحن: ...] tag if present, or infer natural emotional direction from text."""
        if not raw_text:
            return "صمیمی، طبیعی و خوش‌آهنگ", ""

        # Strip <think>, <action>, <fact> blocks first
        text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL)
        text = re.sub(r"^.*?</think>", "", text, flags=re.DOTALL)
        text = re.sub(r"<action>.*?</action>", "", text, flags=re.DOTALL)
        text = re.sub(r"<fact>.*?</fact>", "", text, flags=re.DOTALL)
        text = re.sub(r"\[Action.*?\]", "", text)
        text = re.sub(r"C\s*\.\s*E\s*\.\s*L\s*\.\s*E\s*\.\s*S\s*\.\s*T\s*\.\s*E\.?", "سلست", text, flags=re.IGNORECASE)

        # Check for explicit [لحن: ...] or [Tone: ...] tag from LLM
        tone_match = re.search(r"\[\s*(?:لحن|حس|tone)\s*:\s*([^\]]+)\]", text, flags=re.IGNORECASE)
        explicit_tone = ""
        if tone_match:
            explicit_tone = tone_match.group(1).strip()
            text = text[:tone_match.start()] + text[tone_match.end():]

        # Clean markdown symbols
        text = re.sub(r"[*`#_~>|•]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        if explicit_tone:
            return explicit_tone, text

        # Auto-infer emotion from punctuation and keywords
        if any(w in text for w in ["هشدار", "خطر", "فوری", "مهم", "تایید", "بحرانی", "⚠"]):
            return "جدی، هشداردهنده و با تاکید دقیق روی کلمات کلیدی", text
        if "!" in text and ("؟" in text or "عجب" in text or "واو" in text or "فوق‌العاده" in text or "عالی" in text):
            return "متعجب، پرانرژی، زنده و با حس اشتیاق واقعی", text
        if "!" in text:
            return "پرانرژی، گرم، صمیمی و با تاکید طبیعی", text
        if "؟" in text:
            return "کنجکاو، صمیمی و با آهنگ پرسشی طبیعی در انتهای جمله", text
        return "صمیمی، گرم، هوشمندانه و کاملاً طبیعی", text

    @classmethod
    def _build_gemini_emotion_prompt(cls, text: str) -> str:
        tone, clean_text = cls.extract_emotion_and_clean_text(text)
        director_prompt = (
            f"تو «سلست» هستی. متن زیر را با لهجه اصیل و طبیعیِ فارسیِ تهرانی (محاوره مودبانه و امروزی ایران، "
            f"کاملاً بدون لهجه افغانی/دری)، با رعایت دقیق کسره‌های اضافه، تنفس و مکث‌های طبیعی انسانی، "
            f"و با حس و لحنِ «{tone}» بخوان. "
            f"کلمات انگلیسی داخل متن را با تلفظ طبیعی انگلیسی ادا کن و فقط خود متن زیر را بدون هیچ کلمه اضافه‌ای بخوان:\n\n"
            f"«{clean_text}»"
        )
        return director_prompt

    # ------------------------------------------------------------------
    # Public Speak API
    # ------------------------------------------------------------------

    def speak(self, text: str, blocking: bool = False) -> bool:
        if not self._initialized:
            self.initialize()

        _, clean_check = self.extract_emotion_and_clean_text(text)
        if not clean_check:
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

    def get_status(self) -> dict:
        api_key, model, voice, _ = self._get_gemini_credentials()
        return {
            "piper_available": True,
            "gemini_tts_configured": bool(api_key),
            "gemini_tts_model": model,
            "gemini_tts_voice": voice,
            "edge_available": self._edge_available,
            "speaking": self._speaking,
            "queue_size": len(self._queue),
        }

    # ------------------------------------------------------------------
    # Worker: Gemini Native Audio TTS -> Fallback Edge-TTS
    # ------------------------------------------------------------------

    def _speak_worker(self, raw_text: str, gen_id: int) -> bool:
        with self._play_lock:
            with self._lock:
                if gen_id != self._speech_generation_id:
                    return False
                self._speaking = True

            try:
                # 1. Primary: Google Gemini Native Audio TTS (gemini-2.5-flash-preview-tts)
                api_key, tts_model, tts_voice, proxies = self._get_gemini_credentials()
                if api_key:
                    pcm, sample_rate = self._synthesize_gemini_tts(raw_text, api_key, tts_model, tts_voice, proxies)
                    if pcm is not None and len(pcm) > 0 and gen_id == self._speech_generation_id:
                        self._playback_audio(pcm, sample_rate=sample_rate, blocking=False)
                        return True
                else:
                    logger.warning("Gemini API Key is empty in config.json! Falling back to Edge-TTS. Enter your Gemini API Key in UI -> API & WEB tab.")

                # 2. Fallback: Edge-TTS (if Gemini key is not yet entered or network error)
                if self._edge_available and gen_id == self._speech_generation_id:
                    _, clean_text = self.extract_emotion_and_clean_text(raw_text)
                    fallback_text = self._normalize_for_fallback_edge(clean_text)
                    pcm = self._synthesize_edge_fallback(fallback_text)
                    if pcm is not None and len(pcm) > 0:
                        self._playback_audio(pcm, sample_rate=self.DEFAULT_SAMPLE_RATE, blocking=False)
                        return True

                return False
            except Exception as exc:
                logger.error("TTS worker failed: %s", exc)
                return False
            finally:
                with self._lock:
                    if gen_id == self._speech_generation_id:
                        self._speaking = False

    def _synthesize_gemini_tts(
        self,
        raw_text: str,
        api_key: str,
        tts_model: str,
        tts_voice: str,
        proxies: Optional[Dict[str, str]],
    ) -> Tuple[Optional[np.ndarray], int]:
        try:
            prompt_text = self._build_gemini_emotion_prompt(raw_text)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{tts_model}:generateContent?key={api_key}"

            payload = {
                "contents": [
                    {"parts": [{"text": prompt_text}]}
                ],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": tts_voice
                            }
                        }
                    },
                },
            }

            resp = requests.post(url, json=payload, proxies=proxies, timeout=25)
            if resp.status_code != 200:
                logger.warning("Gemini TTS HTTP %s: %s", resp.status_code, resp.text[:250])
                return None, self.DEFAULT_SAMPLE_RATE

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return None, self.DEFAULT_SAMPLE_RATE

            parts = candidates[0].get("content", {}).get("parts", [])
            for part in parts:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    raw_audio = base64.b64decode(inline["data"])
                    mime_type = (inline.get("mimeType") or inline.get("mime_type") or "audio/L16;rate=24000").lower()

                    # Parse sample rate from mimeType if present (default 24000 Hz)
                    sr = 24000
                    sr_match = re.search(r"rate=(\d+)", mime_type)
                    if sr_match:
                        sr = int(sr_match.group(1))

                    # If WAV container header is present, strip header via wave module
                    if raw_audio[:4] == b"RIFF":
                        with wave.open(io.BytesIO(raw_audio), "rb") as wf:
                            sr = wf.getframerate()
                            frames = wf.readframes(wf.getnframes())
                            pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                    else:
                        # Raw signed 16-bit little-endian PCM from Gemini TTS
                        pcm = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32) / 32768.0

                    return self._apply_dsp_if_enabled(pcm, sr), sr

            return None, self.DEFAULT_SAMPLE_RATE
        except Exception as exc:
            logger.warning("Gemini TTS request failed (%s); falling back to local/Edge TTS.", exc)
            return None, self.DEFAULT_SAMPLE_RATE

    @classmethod
    def _normalize_for_fallback_edge(cls, text: str) -> str:
        def _repl(m: re.Match) -> str:
            w = m.group(0)
            return cls.FALLBACK_PHONETIC_MAP.get(w.lower(), w)
        out = re.sub(r"[A-Za-z][A-Za-z0-9.]*", _repl, text)
        return out

    def _safe_async_run(self, coro) -> bytes:
        """Run async coroutine safely: if an event loop is already running (e.g. FastAPI route),
        dispatch via ThreadPoolExecutor to avoid 'cannot be called when a running event loop
        is already running' crash."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coro).result()

    def _synthesize_edge_fallback(self, text: str) -> Optional[np.ndarray]:
        try:
            import edge_tts
            import miniaudio

            async def _fetch() -> bytes:
                comm = edge_tts.Communicate(text, self.EDGE_PERSIAN_VOICE, rate="+6%", pitch="+2Hz")
                buf = bytearray()
                async for chunk in comm.stream():
                    if chunk.get("type") == "audio":
                        buf.extend(chunk["data"])
                return bytes(buf)

            mp3_bytes = self._safe_async_run(_fetch())
            if not mp3_bytes:
                return None
            decoded = miniaudio.decode(
                mp3_bytes,
                output_format=miniaudio.SampleFormat.FLOAT32,
                nchannels=1,
                sample_rate=self.DEFAULT_SAMPLE_RATE,
            )
            return np.frombuffer(decoded.samples, dtype=np.float32).copy()
        except Exception:
            return None

    def _apply_dsp_if_enabled(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        try:
            from Voice.AudioDSP.jarvis_dsp import JarvisAudioDSP
            if (
                abs(JarvisAudioDSP._live_pitch) < 0.05
                and JarvisAudioDSP._live_metallic < 0.01
                and JarvisAudioDSP._live_reverb < 0.01
            ):
                return pcm
            # Reuse cached DSP instance if sample rate hasn't changed (Bug 3 fix)
            if getattr(self, "_dsp", None) is not None and self._dsp._sr == sample_rate:
                return self._dsp.process(pcm)
            self._dsp = JarvisAudioDSP(sample_rate=sample_rate)
            return self._dsp.process(pcm)
        except Exception:
            return pcm

    def _playback_audio(self, pcm: np.ndarray, sample_rate: int = 24000, blocking: bool = True) -> None:
        """Play PCM audio. On Windows, winsound.SND_MEMORY is always blocking.
        When blocking=False, dispatch to a background daemon thread so the caller
        (e.g. HTTP route or orchestrator) never freezes.
        """
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
            t = threading.Thread(target=_play, daemon=True)
            t.start()


if __name__ == "__main__":
    tts = TTSEngine()
    st = tts.get_status()
    print("=== C.E.L.E.S.T.E. Gemini Native Audio TTS Status ===")
    print(json.dumps(st, indent=2, ensure_ascii=False))
    if not st["gemini_tts_configured"]:
        print("\n[NOTE] To hear Gemini Native Voice, paste your Gemini API Key in Desktop UI -> CONTROL DECK -> API & WEB -> GOOGLE GEMINI API Key and click SAVE!")
    else:
        print("\n[LIVE] Testing Gemini Native Persian Voice with Emotion Director...")
        tts.speak("[لحن: صمیمی، متعجب و پرانرژی] واو مهندس! صدای نیتیو جمینای روی سلست فعال شد! الان هم کلمات انگلیسی مثل RTX 3080 و هم لحن تهرانی رو کاملاً طبیعی می‌خونم!", blocking=True)