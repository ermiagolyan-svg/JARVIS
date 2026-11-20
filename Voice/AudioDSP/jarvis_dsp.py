# C.E.L.E.S.T.E. Voice - Studio-Clean Cyber-Ice Vocal Chain DSP
# Keeps class name JarvisAudioDSP (plus CelesteAudioDSP alias) for full compatibility.
import logging
from pathlib import Path
from typing import Optional

import numpy as np
from scipy.signal import butter, convolve, lfilter, resample_poly, sosfilt

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger(__name__)


class JarvisAudioDSP:
    # Studio-clean vocal mastering chain for C.E.L.E.S.T.E. Persian Neural TTS:
    # - Defaults to 0.0 metallic comb and 0.0 reverb so neural speech is 100% natural, warm, and echo-free.
    # - Applies gentle 75 Hz rumble filter + smooth studio presence (+1.2 dB @ 3.8 kHz) + soft limiter.
    # - Supports live tuning from Desktop UI sliders when the user desires custom effects.

    DEFAULT_PITCH_SEMITONES = 0.0
    DEFAULT_SPEECH_RATE = 1.00
    HPF_CUTOFF_HZ = 75
    COMB_DECAY = 0.05
    COMB_FEEDBACK = 0.12
    PRESENCE_FREQ_HZ = 3800
    PRESENCE_Q = 1.10
    PRESENCE_GAIN_DB = 1.2
    AIR_SHELF_FREQ_HZ = 10000
    AIR_SHELF_GAIN_DB = 1.0
    REVERB_TAIL_SEC = 0.18
    REVERB_DECAY = 0.0
    REVERB_WET_MIX = 0.0
    SAMPLE_RATE_DEFAULT = 24000

    # Shared live configuration updated via REST API (/api/v1/voice/config)
    _live_pitch: float = DEFAULT_PITCH_SEMITONES
    _live_metallic: float = 0.0
    _live_reverb: float = 0.0
    _live_rate: float = DEFAULT_SPEECH_RATE

    def __init__(
        self,
        pitch_semitones: Optional[float] = None,
        sample_rate: int = SAMPLE_RATE_DEFAULT,
        speech_rate: Optional[float] = None,
        metallic_mix: Optional[float] = None,
        reverb_decay: Optional[float] = None,
    ):
        self._pitch = pitch_semitones if pitch_semitones is not None else self._live_pitch
        self._sr = sample_rate
        self._speech_rate = speech_rate if speech_rate is not None else self._live_rate
        self._crystal_mix = metallic_mix if metallic_mix is not None else self._live_metallic
        self._reverb_decay = reverb_decay if reverb_decay is not None else self._live_reverb
        self._comb_delay_samples = max(1, int(0.0025 * self._sr))
        self._reverb_ir = self._build_reverb_ir()

    @classmethod
    def set_global_config(
        cls,
        pitch_semitones: Optional[float] = None,
        metallic_mix: Optional[float] = None,
        reverb_decay: Optional[float] = None,
        speech_rate: Optional[float] = None,
    ):
        if pitch_semitones is not None:
            cls._live_pitch = float(np.clip(pitch_semitones, -4.0, 2.0))
        if metallic_mix is not None:
            cls._live_metallic = float(np.clip(metallic_mix, 0.0, 0.5))
        if reverb_decay is not None:
            cls._live_reverb = float(np.clip(reverb_decay, 0.0, 0.5))
        if speech_rate is not None:
            cls._live_rate = float(np.clip(speech_rate, 0.85, 1.25))

    def update_config(
        self,
        pitch_semitones: Optional[float] = None,
        metallic_mix: Optional[float] = None,
        reverb_decay: Optional[float] = None,
        speech_rate: Optional[float] = None,
    ):
        self.set_global_config(pitch_semitones, metallic_mix, reverb_decay, speech_rate)
        self._pitch = self._live_pitch
        self._crystal_mix = self._live_metallic
        self._reverb_decay = self._live_reverb
        self._speech_rate = self._live_rate
        self._reverb_ir = self._build_reverb_ir()

    def process(self, pcm: np.ndarray) -> np.ndarray:
        if pcm.dtype != np.float32:
            pcm = pcm.astype(np.float32)
        if pcm.ndim == 2:
            pcm = np.mean(pcm, axis=1)

        out = self._hpf(pcm)
        if self._crystal_mix > 0.01:
            out = self._comb_filter(out)
        out = self._presence_eq(out)
        out = self._high_shelf_air_eq(out)
        if abs(self._pitch) >= 0.05 or abs(self._speech_rate - 1.0) >= 0.02:
            out = self._pitch_and_rate_shift(out)
        if self._reverb_decay > 0.01:
            out = self._reverb(out)
        return np.clip(out, -1.0, 1.0).astype(np.float32)

    def process_stereo(self, pcm: np.ndarray) -> np.ndarray:
        if pcm.ndim == 1:
            return self.process(pcm)
        out_l = self.process(pcm[:, 0])
        out_r = self.process(pcm[:, 1])
        max_len = max(len(out_l), len(out_r))
        if len(out_l) < max_len:
            out_l = np.pad(out_l, (0, max_len - len(out_l)), mode='constant')
        if len(out_r) < max_len:
            out_r = np.pad(out_r, (0, max_len - len(out_r)), mode='constant')
        return np.column_stack([out_l, out_r])

    def _hpf(self, x: np.ndarray) -> np.ndarray:
        sos = butter(2, self.HPF_CUTOFF_HZ, btype='high', fs=self._sr, output='sos')
        return sosfilt(sos, x)

    def _comb_filter(self, x: np.ndarray) -> np.ndarray:
        d = self._comb_delay_samples
        b = np.zeros(d + 1)
        b[0] = 1.0
        b[d] = self.COMB_DECAY
        a = np.zeros(d + 1)
        a[0] = 1.0
        a[d] = -self.COMB_FEEDBACK
        wet = lfilter(b, a, x)
        mix = float(np.clip(self._crystal_mix * 0.25, 0.0, 0.15))
        return (1.0 - mix) * x + mix * wet

    def _presence_eq(self, x: np.ndarray) -> np.ndarray:
        nyquist = self._sr / 2.0
        target_hz = min(self.PRESENCE_FREQ_HZ, nyquist * 0.85)
        q_val = self.PRESENCE_Q
        gain_lin = 10 ** (self.PRESENCE_GAIN_DB / 40.0)
        w0 = 2.0 * np.pi * (target_hz / self._sr)
        alpha = np.sin(w0) / (2.0 * q_val)
        b0 = 1.0 + alpha * gain_lin
        b1 = -2.0 * np.cos(w0)
        b2 = 1.0 - alpha * gain_lin
        a0 = 1.0 + alpha / gain_lin
        a1 = -2.0 * np.cos(w0)
        a2 = 1.0 - alpha / gain_lin
        sos = np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])
        return sosfilt(sos, x)

    def _high_shelf_air_eq(self, x: np.ndarray) -> np.ndarray:
        nyquist = self._sr / 2.0
        shelf_hz = min(float(self.AIR_SHELF_FREQ_HZ), nyquist * 0.86)
        w0 = 2.0 * np.pi * (shelf_hz / self._sr)
        cos_w0 = np.cos(w0)
        sin_w0 = np.sin(w0)
        a_lin = 10 ** (self.AIR_SHELF_GAIN_DB / 40.0)
        alpha = (sin_w0 / 2.0) * np.sqrt(2.0)
        two_sqrt_a_alpha = 2.0 * np.sqrt(a_lin) * alpha

        b0 = a_lin * ((a_lin + 1.0) + (a_lin - 1.0) * cos_w0 + two_sqrt_a_alpha)
        b1 = -2.0 * a_lin * ((a_lin - 1.0) + (a_lin + 1.0) * cos_w0)
        b2 = a_lin * ((a_lin + 1.0) + (a_lin - 1.0) * cos_w0 - two_sqrt_a_alpha)
        a0 = (a_lin + 1.0) - (a_lin - 1.0) * cos_w0 + two_sqrt_a_alpha
        a1 = 2.0 * ((a_lin - 1.0) - (a_lin + 1.0) * cos_w0)
        a2 = (a_lin + 1.0) - (a_lin - 1.0) * cos_w0 - two_sqrt_a_alpha

        sos = np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])
        return sosfilt(sos, x)

    def _pitch_and_rate_shift(self, x: np.ndarray) -> np.ndarray:
        pitch_ratio = 2.0 ** (-self._pitch / 12.0)
        effective_resample = pitch_ratio / max(0.85, self._speech_rate)
        if abs(effective_resample - 1.0) < 0.008:
            return x
        num = int(round(effective_resample * 200))
        den = 200
        gcd = np.gcd(num, den)
        num //= gcd
        den //= gcd
        return resample_poly(x, num, den)

    def _reverb(self, x: np.ndarray) -> np.ndarray:
        # Causal convolution (mode='full'[:len(x)]) so pre-echo NEVER occurs
        ir = self._reverb_ir
        if len(ir) == 0 or len(x) == 0:
            return x
        wet = convolve(x, ir, mode='full')[: len(x)]
        wet_mix = float(np.clip(self._reverb_decay * 0.18, 0.0, 0.12))
        return (1.0 - wet_mix) * x + wet_mix * wet

    def _build_reverb_ir(self) -> np.ndarray:
        length = max(64, int(self.REVERB_TAIL_SEC * self._sr))
        ir = np.zeros(length, dtype=np.float32)
        delays = [int(0.012 * self._sr), int(0.024 * self._sr), int(0.037 * self._sr)]
        gains = [0.35, 0.18, 0.08]
        for d, g in zip(delays, gains):
            if d < length:
                ir[d] = g
        # Normalize by L1 sum so wet signal never exceeds dry signal volume
        norm = np.sum(np.abs(ir))
        if norm > 0:
            ir /= norm
        return ir.astype(np.float32)


CelesteAudioDSP = JarvisAudioDSP


if __name__ == '__main__':
    dsp = JarvisAudioDSP()
    sr = dsp._sr
    t = np.linspace(0, 1.0, sr, dtype=np.float32)
    tone = 0.5 * np.sin(2 * np.pi * 440 * t).astype(np.float32)
    out = dsp.process(tone)
    assert len(out) == len(tone)
    print(f'[OK] Studio-Clean C.E.L.E.S.T.E. DSP verified at {sr} Hz.')
    