"""Stage 3.2: STT Engine integration tests with real audio."""
import logging
import numpy as np
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def generate_test_wav(path, duration_sec=3, freq=440, sample_rate=16000):
    """Generate a WAV file with a sine wave tone for pipeline testing."""
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    signal = np.sin(2 * np.pi * freq * t).astype(np.float32)
    pcm = (signal * 32767).astype(np.int16).tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write((36 + len(pcm)).to_bytes(4, "little"))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(b"\x10\x00\x00\x00")
        f.write(b"\x01\x00")
        f.write(b"\x01\x00")
        f.write((sample_rate).to_bytes(4, "little"))
        f.write((sample_rate * 2).to_bytes(4, "little"))
        f.write(b"\x02\x00")
        f.write(b"\x10\x00")
        f.write(b"data")
        f.write(len(pcm).to_bytes(4, "little"))
        f.write(pcm)
    print(f"Generated WAV: {path} ({duration_sec}s, {freq}Hz)")
    return path

def test_engine_import():
    from Voice.STT import STTEngine
    engine = STTEngine(model_size="small", device="cpu", compute_type="int8", cpu_threads=8)
    assert engine._model_size == "small"
    assert engine._device == "cpu"
    assert engine._cpu_threads == 8
    print("PASS: Engine import and defaults")
    return engine

def test_silent_transcription(engine):
    silent_pcm = np.zeros(16000 * 2, dtype=np.int16).tobytes()
    result = engine.transcribe_pcm(silent_pcm, sample_rate=16000)
    assert isinstance(result["text"], str)
    assert isinstance(result["segments"], list)
    assert isinstance(result["language"], str)
    print(f"PASS: Silent transcription -> '{result['text']}' (lang={result['language']})")

def test_wav_transcription(engine, wav_path):
    with open(wav_path, "rb") as f:
        f.seek(44)
        pcm_bytes = f.read()
    result = engine.transcribe_pcm(pcm_bytes, sample_rate=16000, language="en")
    print(f"PASS: WAV transcription -> '{result['text']}' ({len(result['segments'])} segments)")
    return result

def main():
    from Voice.STT import STTEngine
    test_dir = Path(__file__).parent / "test_data"
    test_dir.mkdir(exist_ok=True)

    print("\n" + "=" * 60)
    print("Stage 3.2: STT Engine Integration Tests")
    print("=" * 60)

    engine = test_engine_import()
    ok = engine.initialize()
    print(f"PASS: Initialization {'OK' if ok else 'FAILED'}")
    test_silent_transcription(engine)

    wav_path = test_dir / "test_tone.wav"
    generate_test_wav(wav_path, duration_sec=3, freq=440)
    test_wav_transcription(engine, wav_path)

    engine.shutdown()
    print("=" * 60)
    print("All Stage 3.2 tests complete.")
    print("=" * 60)

if __name__ == "__main__":
    main()
