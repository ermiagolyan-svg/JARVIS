# C.E.L.E.S.T.E. Voice - Cyber-Ice Vocal Chain DSP
# Keeps class name JarvisAudioDSP (plus CelesteAudioDSP alias) for full compatibility.
import logging
from pathlib import Path
from typing import Optional

import numpy as np
from scipy.signal import butter, convolve, lfilter, resample_poly, sosfilt

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger(__name__)


class JarvisAudioDSP:
    # Applies the C.E.L.E.S.T.E. Cyber-Ice Vocal Chain to raw TTS audio streams:
    #   1. High-Pass Filter @ 95 Hz (4th-order Butterworth - removes boxy low-end)
    #   2. Subtle Crystal Harmonic Shimmer (10% mix)
    #   3. Presence Peaking EQ: +2.5 dB @ 3.5 kHz (crisp articulation)
    #   4. High-Shelf Air EQ: +2.0 dB @ 10 kHz+ (crystalline Cyber-Ice shimmer)
    #   5. Pitch Shift (-0.3 semitones default) & Speech Rate (1.06x)
    #   6. Tight Studio Room Early Reflections Reverb (0.30s decay, 10% wet / 90% dry)

    DEFAULT_PITCH_SEMITONES = -0.3
    DEFAULT_SPEECH_RATE = 1.06
    HPF_CUTOFF_HZ = 95
    COMB_DECAY = 0.08
    COMB_FEEDBACK = 0.22
    PRESENCE_FREQ_HZ = 3500
    PRESENCE_Q = 1.35
    PRESENCE_GAIN_DB = 2.5
    AIR_SHELF_FREQ_HZ = 10000
    AIR_SHELF_GAIN_DB = 2.0
    REVERB_TAIL_SEC = 0.30
    REVERB_DECAY = 0.30
    REVERB_WET_MIX = 0.10
    SAMPLE_RATE_DEFAULT = 22050

    def __init__(
        self,
        pitch_semitones: Optional[float] = None,
        sample_rate: int = SAMPLE_RATE_DEFAULT,
        speech_rate: Optional[float] = None,
        metallic_mix: Optional[float] = None,
        reverb_decay: Optional[float] = None,
    ):
        self._pitch = pitch_semitones if pitch_semitones is not None else self.DEFAULT_PITCH_SEMITONES
        self._sr = sample_rate
        self._speech_rate = speech_rate if speech_rate is not None else self.DEFAULT_SPEECH_RATE
        self._crystal_mix = metallic_mix if metallic_mix is not None else 0.10
        self._reverb_decay = reverb_decay if reverb_decay is not None else self.REVERB_DECAY
        self._comb_delay_samples = max(1, int(0.0032 * self._sr))
        self._reverb_ir = self._build_reverb_ir()
        logger.info(
            'Celeste Cyber-Ice DSP initialized: pitch=%.2f st, rate=%.2fx, sr=%d',
            self._pitch, self._speech_rate, self._sr,
        )

    def update_config(
        self,
        pitch_semitones: Optional[float] = None,
        metallic_mix: Optional[float] = None,
        reverb_decay: Optional[float] = None,
        speech_rate: Optional[float] = None,
    ):
        if pitch_semitones is not None:
            self._pitch = float(pitch_semitones)
        if metallic_mix is not None:
            self._crystal_mix = float(np.clip(metallic_mix, 0.0, 0.6))
        if reverb_decay is not None:
            self._reverb_decay = float(np.clip(reverb_decay, 0.05, 0.8))
            self._reverb_ir = self._build_reverb_ir()
        if speech_rate is not None:
            self._speech_rate = float(np.clip(speech_rate, 0.85, 1.30))

    def process(self, pcm: np.ndarray) -> np.ndarray:
        if pcm.dtype != np.float32:
            pcm = pcm.astype(np.float32)
        if pcm.ndim == 2:
            pcm = np.mean(pcm, axis=1)

        out = self._hpf(pcm)
        out = self._comb_filter(out)
        out = self._presence_eq(out)
        out = self._high_shelf_air_eq(out)
        out = self._pitch_and_rate_shift(out)
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
        sos = butter(4, self.HPF_CUTOFF_HZ, btype='high', fs=self._sr, output='sos')
        return sosfilt(sos, x)

    def _comb_filter(self, x: np.ndarray) -> np.ndarray:
        if self._crystal_mix <= 0.001:
            return x
        d = self._comb_delay_samples
        b = np.zeros(d + 1)
        b[0] = 1.0
        b[d] = self.COMB_DECAY
        a = np.zeros(d + 1)
        a[0] = 1.0
        a[d] = -self.COMB_FEEDBACK
        wet = lfilter(b, a, x)
        return (1.0 - self._crystal_mix) * x + self._crystal_mix * wet

    def _presence_eq(self, x: np.ndarray) -> np.ndarray:
        # +2.5 dB Peaking EQ @ 3.5 kHz for crisp consonant presence
        nyquist = self._sr / 2.0
        target_hz = min(self.PRESENCE_FREQ_HZ, nyquist * 0.85)
        freq = target_hz / nyquist
        q_val = self.PRESENCE_Q
        gain_lin = 10 ** (self.PRESENCE_GAIN_DB / 40.0)
        w0 = 2.0 * np.pi * freq
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
        # +2.0 dB High-Shelf EQ @ 10 kHz+ (clamped safely below Nyquist)
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
        # Combines subtle pitch shift (-0.3 st) with speech rate (1.06x)
        pitch_ratio = 2.0 ** (self._pitch / 12.0)
        effective_resample = pitch_ratio / max(0.8, self._speech_rate)
        if abs(effective_resample - 1.0) < 0.005:
            return x.copy()
        num = int(round(effective_resample * 1000))
        den = 1000
        gcd = np.gcd(num, den)
        num //= gcd
        den //= gcd
        return resample_poly(x, num, den)

    def _reverb(self, x: np.ndarray) -> np.ndarray:
        # Tight Studio Room: 90% dry + 10% early reflections wet
        ir = self._reverb_ir[: len(x)]
        if len(ir) == 0:
            return x
        wet = convolve(x, ir, mode='same')
        wet_mix = float(np.clip(self.REVERB_WET_MIX, 0.0, 0.35))
        return (1.0 - wet_mix) * x + wet_mix * wet

    def _build_reverb_ir(self) -> np.ndarray:
        # Tight studio room early reflections (0.30s decay)
        length = max(64, int(self.REVERB_TAIL_SEC * self._sr))
        ir = np.zeros(length, dtype=np.float32)
        delays = [
            int(0.011 * self._sr),
            int(0.019 * self._sr),
            int(0.029 * self._sr),
            int(0.041 * self._sr),
        ]
        gains = [0.60, 0.42, 0.28, 0.16]
        for d, g in zip(delays, gains):
            if d < length:
                ir[d] += g

        t = np.linspace(0.0, self.REVERB_TAIL_SEC, length, dtype=np.float32)
        envelope = np.exp(-t / max(0.05, self._reverb_decay * 0.25))
        rng = np.random.default_rng(42)
        early_diffusion = rng.normal(0.0, 0.06, size=length).astype(np.float32) * envelope
        ir = ir + early_diffusion

        peak = np.max(np.abs(ir))
        if peak > 0:
            ir /= peak
        return ir.astype(np.float32)


CelesteAudioDSP = JarvisAudioDSP


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    print('=' * 60)
    print('C.E.L.E.S.T.E. Cyber-Ice Vocal Chain DSP - Self Test')
    print('=' * 60)
    dsp = JarvisAudioDSP()

    sr = dsp._sr
    t = np.linspace(0, 1.5, int(sr * 1.5), dtype=np.float32)
    test_tone = 0.6 * np.sin(2 * np.pi * 440 * t).astype(np.float32)

    processed = dsp.process(test_tone)
    print(f'Input samples:  {len(test_tone)} @ {sr} Hz')
    print(f'Output samples: {len(processed)} (rate={dsp._speech_rate}x, pitch={dsp._pitch} st)')
    print(f'RMS input:      {np.sqrt(np.mean(test_tone**2)):.4f}')
    print(f'RMS output:     {np.sqrt(np.mean(processed**2)):.4f}')

    stereo_test = np.column_stack([test_tone, test_tone * 0.8])
    processed_stereo = dsp.process_stereo(stereo_test)
    assert processed_stereo.ndim == 2
    print('[OK] Cyber-Ice Vocal Chain DSP self-test passed.')