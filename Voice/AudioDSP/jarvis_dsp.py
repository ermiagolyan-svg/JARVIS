"""JARVIS Voice - Jarvis Metallic Audio DSP (Stage 3.2)."""
import logging
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
from scipy.signal import butter, sosfilt, resample_poly, lfilter, convolve

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger(__name__)


class JarvisAudioDSP:
    """Applies JARVIS metallic voice signature to raw audio streams.

    Pipeline order (input -> output):
        1. High-pass filter @95 Hz
        2. Metallic resonator comb filter
        3. Presence boost EQ (~3 kHz peaking)
        4. Pitch shift (-2.5 semitones default)
        5. Subtle reverb tail
    """

    DEFAULT_PITCH_SEMITONES = -2.5
    HPF_CUTOFF_HZ = 95
    COMB_DECAY = 0.18
    COMB_FEEDBACK = 0.45
    PRESENCE_FREQ_HZ = 3000
    PRESENCE_Q = 1.4
    PRESENCE_GAIN_DB = 4.5
    REVERB_TAIL_SEC = 0.22
    REVERB_DECAY = 0.6
    SAMPLE_RATE_DEFAULT = 22050

    def __init__(
        self,
        pitch_semitones: Optional[float] = None,
        sample_rate: int = SAMPLE_RATE_DEFAULT,
    ):
        self._pitch = pitch_semitones if pitch_semitones is not None else self.DEFAULT_PITCH_SEMITONES
        self._sr = sample_rate
        self._comb_delay_samples = int(0.004 * self._sr)  # ~4 ms metallic ring
        self._reverb_ir = self._build_reverb_ir()
        logger.info(
            "JarvisAudioDSP created: pitch=%.1f st, sr=%d, comb_delay=%d",
            self._pitch, self._sr, self._comb_delay_samples,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, pcm: np.ndarray) -> np.ndarray:
        """Apply full JARVIS DSP chain to PCM array (float32, mono or stereo)."""
        if pcm.dtype != np.float32:
            pcm = pcm.astype(np.float32)
        if pcm.ndim == 2:
            # Stereo -> mono mix
            pcm = np.mean(pcm, axis=1)

        out = self._hpf(pcm)
        out = self._comb_filter(out)
        out = self._presence_eq(out)
        out = self._pitch_shift(out)
        out = self._reverb(out)
        # Clamp to prevent clipping
        return np.clip(out, -1.0, 1.0).astype(np.float32)

    def process_stereo(self, pcm: np.ndarray) -> np.ndarray:
        """Process each channel independently for stereo output.

        Handles length changes from pitch-shift by padding shorter
        channels to match the longer one.
        """
        if pcm.ndim == 1:
            return self.process(pcm)
        out_l = self.process(pcm[:, 0])
        out_r = self.process(pcm[:, 1])
        max_len = max(len(out_l), len(out_r))
        if len(out_l) < max_len:
            out_l = np.pad(out_l, (0, max_len - len(out_l)), mode="constant")
        if len(out_r) < max_len:
            out_r = np.pad(out_r, (0, max_len - len(out_r)), mode="constant")
        return np.column_stack([out_l, out_r])

    # ------------------------------------------------------------------
    # Internal DSP stages
    # ------------------------------------------------------------------

    def _hpf(self, x: np.ndarray) -> np.ndarray:
        """High-pass filter at 95 Hz (4th-order Butterworth)."""
        sos = butter(4, self.HPF_CUTOFF_HZ, btype="high", fs=self._sr, output="sos")
        return sosfilt(sos, x)

    def _comb_filter(self, x: np.ndarray) -> np.ndarray:
        """Metallic resonator comb filter.

        A short-delay feedback comb filter creates a ring of harmonics
        that mimics the metallic timbre of JARVIS's voice.
        """
        d = self._comb_delay_samples
        b = np.zeros(d + 1)
        b[0] = 1.0
        b[d] = self.COMB_DECAY
        a = np.zeros(d + 1)
        a[0] = 1.0
        a[d] = -self.COMB_FEEDBACK
        return lfilter(b, a, x)

    def _presence_eq(self, x: np.ndarray) -> np.ndarray:
        """Peaking EQ around PRESENCE_FREQ_HZ for vocal clarity."""
        freq = self.PRESENCE_FREQ_HZ / (self._sr / 2.0)
        Q = self.PRESENCE_Q
        gain_lin = 10 ** (self.PRESENCE_GAIN_DB / 40.0)
        w0 = 2 * np.pi * freq
        alpha = np.sin(w0) / (2 * Q)
        b0 = 1 + alpha * gain_lin
        b1 = -2 * np.cos(w0)
        b2 = 1 - alpha * gain_lin
        a0 = 1 + alpha / gain_lin
        a1 = -2 * np.cos(w0)
        a2 = 1 - alpha / gain_lin
        sos = np.array([[b0/a0, b1/a0, b2/a0, 1, a1/a0, a2/a0]])
        return sosfilt(sos, x)

    def _pitch_shift(self, x: np.ndarray) -> np.ndarray:
        """Pitch shift using resample_poly (phase-vocoder-free, fast)."""
        if abs(self._pitch) < 0.01:
            return x.copy()
        ratio = 2 ** (self._pitch / 12.0)
        num = int(round(ratio * 1000))
        den = 1000
        gcd = np.gcd(num, den)
        num //= gcd
        den //= gcd
        return resample_poly(x, num, den)

    def _reverb(self, x: np.ndarray) -> np.ndarray:
        """Subtle convolution reverb tail."""
        ir = self._reverb_ir[: len(x)]
        out = convolve(x, ir, mode="same")
        return out * 0.35  # blend dry/wet at ~28% wet

    def _build_reverb_ir(self) -> np.ndarray:
        """Synthesize a short metallic reverb impulse response."""
        length = int(self.REVERB_TAIL_SEC * self._sr)
        ir = np.zeros(length, dtype=np.float32)
        delays = [int(0.017 * self._sr), int(0.023 * self._sr),
                  int(0.031 * self._sr), int(0.044 * self._sr)]
        gains = [0.5, 0.35, 0.28, 0.18]
        for d, g in zip(delays, gains):
            if d < length:
                ir[d] += g
        tail = ir.copy()
        for i in range(1, len(tail)):
            tail[i] *= self.REVERB_DECAY
        ir += tail * 0.15
        peak = np.max(np.abs(ir))
        if peak > 0:
            ir /= peak
        return ir


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("=" * 60)
    print("JARVIS Audio DSP - Self Test (Stage 3.2)")
    print("=" * 60)
    dsp = JarvisAudioDSP(pitch_semitones=-2.5)

    sr = dsp._sr
    t = np.linspace(0, 1.5, int(sr * 1.5), dtype=np.float32)
    test_tone = 0.6 * np.sin(2 * np.pi * 440 * t).astype(np.float32)

    print(f"Input: {len(test_tone)} samples @ {sr} Hz, 440 Hz sine")
    processed = dsp.process(test_tone)
    print(f"Output: {len(processed)} samples (clamped)")
    print(f"RMS input:  {np.sqrt(np.mean(test_tone**2)):.4f}")
    print(f"RMS output: {np.sqrt(np.mean(processed**2)):.4f}")
    print(f"Peak input:  {np.max(np.abs(test_tone)):.4f}")
    print(f"Peak output: {np.max(np.abs(processed)):.4f}")

    stereo_test = np.column_stack([test_tone, test_tone * 0.8])
    processed_stereo = dsp.process_stereo(stereo_test)
    print(f"Stereo: {processed_stereo.shape} -> OK")
    print("=" * 60)
    print("DSP self-test complete.")
    print("=" * 60)
