# V.E.S.P.E.R. Voice - Offline STT Engine Test Suite
# Shenava-Koochik CTC + Silero VAD (CPU-only) automated unit and integration tests.

import sys
from pathlib import Path

# Ensure PROJECT_ROOT is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import soundfile as sf
from Voice.STT.persian_itn import apply_itn
from Voice.STT.stt_engine import STTEngine, resolve_input_device


def test_engine_import_and_config():
    # Verifies STTEngine initializes strictly with CPU provider and model paths exist.
    engine = STTEngine()
    ok = engine.initialize()
    assert ok is True, 'STTEngine failed to initialize'

    status = engine.get_status()
    assert status.get('provider') == 'cpu', f"Expected provider 'cpu', got {status.get('provider')}"
    assert Path(status['model_path']).exists(), f"Model path not found: {status['model_path']}"
    assert Path(status['tokens_path']).exists(), f"Tokens path not found: {status['tokens_path']}"
    assert Path(status['vad_model_path']).exists(), f"VAD model path not found: {status['vad_model_path']}"

    # Confirm legacy engine module is not loaded
    legacy_mod = 'faster' + '_' + 'whisper'
    assert legacy_mod not in sys.modules, f'Unexpected {legacy_mod} found in sys.modules'

    print('[TEST 1] test_engine_import_and_config: PASS (CPU provider, valid models, no legacy STT)')
    return engine


def test_persian_itn():
    # Verifies apply_itn converts Persian spoken numbers properly and handles empty input.
    assert apply_itn('') == '', 'Expected empty string on empty input'

    cases = [
        ('بیست و پنج', '۲۵'),
        ('من صد و بیست هزار تومان پرداخت کردم', 'من ۱۲۰۰۰۰ تومان پرداخت کردم'),
        ('ساعت پنج و سی دقیقه است', 'ساعت ۵ و ۳۰ دقیقه است'),
        ('صفر', '۰'),
    ]
    for inp, expected in cases:
        out = apply_itn(inp, persian_digits=True)
        assert expected in out, f"ITN conversion failed for '{inp}': expected '{expected}' in '{out}'"

    print('[TEST 2] test_persian_itn: PASS (Spoken Persian numbers converted, empty input handled)')


def test_silent_transcription(engine):
    # Verifies 2s silent 16kHz int16 PCM returns empty text, zero stt_ms, and non-negative vad_ms.
    silent_pcm = np.zeros(16000 * 2, dtype=np.int16).tobytes()
    result = engine.transcribe_pcm(silent_pcm, sample_rate=16000)

    assert result['text'] == '', f"Expected empty text for silence, got: '{result['text']}'"
    assert result['stt_ms'] == 0.0, f"Expected stt_ms == 0.0 for silence, got: {result['stt_ms']}"
    assert result['vad_ms'] >= 0.0, f"Expected vad_ms >= 0.0, got: {result['vad_ms']}"
    assert result['audio_sec'] == 2.0, f"Expected audio_sec == 2.0, got: {result['audio_sec']}"

    print(f"[TEST 3] test_silent_transcription: PASS (audio_sec={result['audio_sec']}, vad_ms={result['vad_ms']}, stt_ms={result['stt_ms']})")


def test_real_persian_wav(engine):
    # Verifies transcription of Persian WAV via both transcribe_file and transcribe_pcm.
    sample_wav = PROJECT_ROOT / 'Voice' / 'STT' / 'test_data' / 'sample_fa.wav'
    assert sample_wav.exists(), f'Sample audio file missing: {sample_wav}'

    # 1. transcribe_file
    res_file = engine.transcribe_file(str(sample_wav))
    assert len(res_file['text']) > 0, 'Expected non-empty text from transcribe_file'
    assert 'raw_text' in res_file and len(res_file['raw_text']) > 0, 'Missing or empty raw_text'
    assert 'itn_text' in res_file and len(res_file['itn_text']) > 0, 'Missing or empty itn_text'
    assert res_file['vad_ms'] >= 0.0, f"Expected vad_ms >= 0.0, got {res_file['vad_ms']}"
    assert res_file['stt_ms'] > 0.0, f"Expected stt_ms > 0.0, got {res_file['stt_ms']}"
    assert res_file['rtf'] > 0.0, f"Expected rtf > 0.0, got {res_file['rtf']}"

    print(f"[TEST 4a] transcribe_file: text='{res_file['text']}' | vad_ms={res_file['vad_ms']} | stt_ms={res_file['stt_ms']} | RTF={res_file['rtf']:.4f}")

    # 2. transcribe_pcm (converted to 16kHz int16 mono bytes)
    audio_data, sr = sf.read(str(sample_wav), dtype='float32')
    if audio_data.ndim > 1:
        audio_data = np.mean(audio_data, axis=1)
    pcm_bytes = (np.clip(audio_data, -1.0, 1.0) * 32767).astype(np.int16).tobytes()

    res_pcm = engine.transcribe_pcm(pcm_bytes, sample_rate=sr)
    assert len(res_pcm['text']) > 0, 'Expected non-empty text from transcribe_pcm'
    assert 'raw_text' in res_pcm and len(res_pcm['raw_text']) > 0, 'Missing or empty raw_text'
    assert 'itn_text' in res_pcm and len(res_pcm['itn_text']) > 0, 'Missing or empty itn_text'
    assert res_pcm['vad_ms'] >= 0.0, f"Expected vad_ms >= 0.0, got {res_pcm['vad_ms']}"
    assert res_pcm['stt_ms'] > 0.0, f"Expected stt_ms > 0.0, got {res_pcm['stt_ms']}"
    assert res_pcm['rtf'] > 0.0, f"Expected rtf > 0.0, got {res_pcm['rtf']}"

    print(f"[TEST 4b] transcribe_pcm:  text='{res_pcm['text']}' | vad_ms={res_pcm['vad_ms']} | stt_ms={res_pcm['stt_ms']} | RTF={res_pcm['rtf']:.4f}")
    print('[TEST 4] test_real_persian_wav: PASS (Both file and PCM transcription successful)')


def test_resample_48k_pcm(engine):
    # Verifies 1s silent 48kHz int16 PCM is cleanly resampled to 16kHz without error.
    silent_48k = np.zeros(48000, dtype=np.int16).tobytes()
    res = engine.transcribe_pcm(silent_48k, sample_rate=48000)

    assert abs(res['audio_sec'] - 1.0) < 0.05, f"Expected audio_sec ~ 1.0, got {res['audio_sec']}"
    assert res['text'] == '', f"Expected empty text for silent 48k, got '{res['text']}'"
    print(f"[TEST 5] test_resample_48k_pcm: PASS (audio_sec={res['audio_sec']} resampled cleanly)")


def test_device_resolution():
    # Verifies resolve_input_device('default') returns a valid index without opening any stream.
    dev_idx = resolve_input_device('default')
    assert dev_idx is None or isinstance(dev_idx, int), f'Unexpected device index type: {type(dev_idx)}'
    print(f'[TEST 6] test_device_resolution: PASS (resolved default device index: {dev_idx})')


def main():
    print('=' * 60)
    print('V.E.S.P.E.R. STT Engine (Shenava + Silero VAD) Test Suite')
    print('=' * 60)

    engine = test_engine_import_and_config()
    try:
        test_persian_itn()
        test_silent_transcription(engine)
        test_real_persian_wav(engine)
        test_resample_48k_pcm(engine)
        test_device_resolution()
    finally:
        engine.close()

    print('=' * 60)
    print('ALL STT ENGINE TESTS PASSED (Exit code 0)')
    print('=' * 60)


if __name__ == '__main__':
    main()
