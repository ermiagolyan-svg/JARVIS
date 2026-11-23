# V.E.S.P.E.R. Voice - Offline STT Engine (Shenava-Koochik + Silero VAD)
# 100% CPU-only speech recognition and voice activity detection using sherpa-onnx.

import inspect
import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import sherpa_onnx

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Core.Logger.logger import get_logger, setup_unified_logging
from Voice.STT.persian_itn import apply_itn

setup_unified_logging()
logger = get_logger('stt_engine')

CONFIG_PATH = PROJECT_ROOT / 'Configuration' / 'config.json'


def resolve_input_device(
    input_device: Any = 'default',
    input_device_name: str = '',
    prefer_wasapi: bool = True,
) -> Optional[int]:
    # Resolves resilient microphone input device index without hardcoding.
    try:
        import sounddevice as sd
    except ImportError:
        logger.warning('sounddevice not installed; cannot resolve audio input device')
        return None

    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception as exc:
        logger.warning('Failed to query audio devices: %s', exc)
        return None

    def is_wasapi(hostapi_idx: int) -> bool:
        if 0 <= hostapi_idx < len(hostapis):
            return 'wasapi' in hostapis[hostapi_idx]['name'].lower()
        return False

    # 1. Search by name if provided
    if input_device_name:
        candidates = []
        for idx, dev in enumerate(devices):
            if dev.get('max_input_channels', 0) > 0:
                name = dev.get('name', '')
                if input_device_name.lower() in name.lower():
                    candidates.append((idx, dev))
        if candidates:
            if prefer_wasapi:
                wasapi_candidates = [c for c in candidates if is_wasapi(c[1].get('hostapi', -1))]
                if wasapi_candidates:
                    logger.info("Found input device matching '%s' on WASAPI (index %d)", input_device_name, wasapi_candidates[0][0])
                    return wasapi_candidates[0][0]
            logger.info("Found input device matching '%s' (index %d)", input_device_name, candidates[0][0])
            return candidates[0][0]
        logger.warning("Input device '%s' not found, falling back to default", input_device_name)

    # 2. Explicit integer index supplied
    if isinstance(input_device, int) and not isinstance(input_device, bool):
        if 0 <= input_device < len(devices) and devices[input_device].get('max_input_channels', 0) > 0:
            return input_device
        logger.warning('Supplied device index %s is invalid or has no input channels; falling back to default', input_device)

    # 3. Default device resolution
    try:
        default_idx = sd.default.device[0]
        if default_idx is None or default_idx < 0:
            default_idx = 0
    except Exception:
        default_idx = 0

    if default_idx >= len(devices) or devices[default_idx].get('max_input_channels', 0) <= 0:
        for idx, dev in enumerate(devices):
            if dev.get('max_input_channels', 0) > 0:
                default_idx = idx
                break

    if not prefer_wasapi or default_idx >= len(devices):
        return default_idx

    # If prefer_wasapi is True and default_idx is not WASAPI, try to map to WASAPI counterpart
    curr_hostapi = devices[default_idx].get('hostapi', -1)
    if is_wasapi(curr_hostapi):
        return default_idx

    target_name = devices[default_idx].get('name', '')
    prefix = target_name[:24].lower()
    for idx, dev in enumerate(devices):
        if dev.get('max_input_channels', 0) > 0 and is_wasapi(dev.get('hostapi', -1)):
            dev_name = dev.get('name', '').lower()
            if dev_name == target_name.lower() or dev_name.startswith(prefix) or prefix in dev_name:
                logger.info(
                    "Mapped default input device %d ('%s') to WASAPI device %d ('%s')",
                    default_idx, target_name, idx, dev.get('name'),
                )
                return idx

    return default_idx


def open_resilient_stream(
    device_idx: Optional[int],
    target_sr: int = 16000,
    callback: Any = None,
) -> Tuple[Any, int, Optional[int]]:
    # Opens resilient sounddevice InputStream, validating and falling back across candidate devices,
    # host APIs (WASAPI/DirectSound/MME), channel modes, and sample rates.
    import sounddevice as sd

    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception as exc:
        raise RuntimeError(f'Failed to query sound devices: {exc}')

    def is_api(hostapi_idx: int, api_name_substr: str) -> bool:
        if 0 <= hostapi_idx < len(hostapis):
            return api_name_substr.lower() in hostapis[hostapi_idx].get('name', '').lower()
        return False

    candidate_devices: List[int] = []

    # 1. First: device_idx (e.g., mapped WASAPI device)
    if device_idx is not None and isinstance(device_idx, int) and 0 <= device_idx < len(devices):
        if devices[device_idx].get('max_input_channels', 0) > 0:
            candidate_devices.append(device_idx)

    # 2. Second: Windows default input device sd.default.device[0] if different from device_idx
    try:
        default_dev = sd.default.device[0]
        if default_dev is not None and 0 <= default_dev < len(devices):
            if devices[default_dev].get('max_input_channels', 0) > 0:
                if default_dev not in candidate_devices:
                    candidate_devices.append(default_dev)
    except Exception:
        pass

    # 3. Third (if still different): DirectSound counterpart matching device name prefix (first 24 chars)
    ref_name = ''
    if candidate_devices:
        ref_name = devices[candidate_devices[0]].get('name', '')
    if ref_name:
        prefix = ref_name[:24].lower()
        for idx, dev in enumerate(devices):
            if dev.get('max_input_channels', 0) > 0 and is_api(dev.get('hostapi', -1), 'directsound'):
                dev_name = dev.get('name', '').lower()
                if dev_name == ref_name.lower() or dev_name.startswith(prefix) or prefix in dev_name:
                    if idx not in candidate_devices:
                        candidate_devices.append(idx)
                        break

    # Fallback if candidate_devices is still empty
    if not candidate_devices:
        for idx, dev in enumerate(devices):
            if dev.get('max_input_channels', 0) > 0:
                candidate_devices.append(idx)
                break

    last_exc = None
    for cand in candidate_devices:
        try:
            dev_info = sd.query_devices(cand, 'input')
        except Exception:
            dev_info = devices[cand] if 0 <= cand < len(devices) else {}

        dev_name = dev_info.get('name', str(cand))
        native_sr = int(round(dev_info.get('default_samplerate', target_sr)))
        max_ch = int(dev_info.get('max_input_channels', 1))

        api_name = 'Unknown'
        hostapi_idx = dev_info.get('hostapi', -1)
        if 0 <= hostapi_idx < len(hostapis):
            api_name = hostapis[hostapi_idx].get('name', 'Unknown')
        is_wasapi = 'wasapi' in api_name.lower()

        channel_attempts = [1]
        if max_ch >= 2 and 2 not in channel_attempts:
            channel_attempts.append(2)
        if max_ch > 2 and max_ch not in channel_attempts:
            channel_attempts.append(max_ch)

        sr_attempts = [target_sr]
        if native_sr > 0 and native_sr not in sr_attempts:
            sr_attempts.append(native_sr)
        for common_sr in (48000, 44100):
            if common_sr not in sr_attempts:
                sr_attempts.append(common_sr)

        extra_settings_list = [None]
        if is_wasapi and hasattr(sd, 'WasapiSettings'):
            try:
                extra_settings_list = [sd.WasapiSettings(auto_convert=True), None]
            except Exception:
                extra_settings_list = [None]

        for extra_settings in extra_settings_list:
            for ch in channel_attempts:
                for sr in sr_attempts:
                    stream = None
                    try:
                        stream = sd.InputStream(
                            device=cand,
                            samplerate=sr,
                            channels=ch,
                            dtype='float32',
                            blocksize=0,
                            callback=callback,
                            extra_settings=extra_settings,
                        )
                        stream.start()
                        logger.info(
                            'Opened resilient InputStream | device=%s (%s) | api=%s | sr=%dHz | channels=%d',
                            cand, dev_name, api_name, sr, ch,
                        )
                        return stream, sr, cand
                    except Exception as exc:
                        last_exc = exc
                        if stream is not None:
                            try:
                                stream.close()
                            except Exception:
                                pass
                        logger.warning(
                            'Failed to open InputStream on device %s (%s) | api=%s | sr=%d | ch=%d | err=%s',
                            cand, dev_name, api_name, sr, ch, exc,
                        )
                        continue

    raise RuntimeError(f'Could not open audio InputStream after multiple attempts: {last_exc}')


class STTEngine:
    # Shenava-Koochik CTC + Silero VAD CPU-only STT Engine with Persian ITN.

    def __init__(self, config_path: Any = None, num_threads: Optional[int] = None, device: str = 'cpu', **legacy_kwargs):
        if device != 'cpu':
            logger.info("STTEngine forces CPU execution. Ignoring requested device='%s' (using CPU)", device)

        self._config_path = Path(config_path) if config_path else CONFIG_PATH
        self._infer_lock = threading.Lock()
        self._initialized = False
        self._vad = None
        self._recognizer = None
        self._sos = None

        # Legacy backward-compatibility attributes
        self._device = 'cpu'
        self._model_size = legacy_kwargs.get('model_size', 'small')
        self._compute_type = legacy_kwargs.get('compute_type', 'int8')

        defaults = {
            'engine': 'shenava_sherpa_onnx',
            'provider': 'cpu',
            'model_dir': 'Data/Models/stt/shenava',
            'model_path': 'Data/Models/stt/shenava/model.onnx',
            'tokens_path': 'Data/Models/stt/shenava/tokens.txt',
            'vad_model_path': 'Data/Models/stt/shenava/silero_vad.onnx',
            'sample_rate': 16000,
            'num_threads': 4,
            'vad_num_threads': 1,
            'vad_threshold': 0.35,
            'vad_min_silence_duration': 0.65,
            'vad_min_speech_duration': 0.20,
            'vad_max_speech_duration': 15.0,
            'vad_window_size': 512,
            'vad_buffer_size_seconds': 60,
            'pad_ms': 100,
            'gain': 1.0,
            'highpass_enabled': False,
            'highpass_cutoff_hz': 70.0,
            'enable_itn': True,
            'itn_persian_digits': True,
            'input_device': 'default',
            'input_device_name': '',
            'prefer_wasapi': True,
            'stall_timeout_sec': 3.0,
        }

        cfg_stt = {}
        if self._config_path.exists():
            try:
                with open(self._config_path, 'r', encoding='utf-8') as f:
                    raw_cfg = json.load(f)
                    cfg_stt = raw_cfg.get('stt', {})
            except Exception as exc:
                logger.warning('Failed to load stt config from %s: %s', self._config_path, exc)

        merged = {**defaults, **cfg_stt}

        def resolve_p(p_str: str) -> Path:
            p = Path(p_str)
            return (PROJECT_ROOT / p) if not p.is_absolute() else p

        self._model_path = resolve_p(merged['model_path'])
        self._tokens_path = resolve_p(merged['tokens_path'])
        self._vad_model_path = resolve_p(merged['vad_model_path'])
        self._sample_rate = int(merged['sample_rate'])

        if num_threads is not None:
            self._num_threads = int(num_threads)
        elif 'cpu_threads' in legacy_kwargs and legacy_kwargs['cpu_threads'] is not None:
            self._num_threads = int(legacy_kwargs['cpu_threads'])
        else:
            self._num_threads = int(merged['num_threads'])
        self._cpu_threads = self._num_threads

        self._vad_num_threads = int(merged['vad_num_threads'])
        self._vad_threshold = float(merged['vad_threshold'])
        self._vad_min_silence_duration = float(merged['vad_min_silence_duration'])
        self._vad_min_speech_duration = float(merged['vad_min_speech_duration'])
        self._vad_max_speech_duration = float(merged['vad_max_speech_duration'])
        self._vad_window_size = int(merged['vad_window_size'])
        self._vad_buffer_size_seconds = float(merged['vad_buffer_size_seconds'])
        self._pad_ms = float(merged['pad_ms'])
        self._gain = float(merged['gain'])
        self._highpass_enabled = bool(merged['highpass_enabled'])
        self._highpass_cutoff_hz = float(merged['highpass_cutoff_hz'])
        self._enable_itn = bool(merged['enable_itn'])
        self._itn_persian_digits = bool(merged['itn_persian_digits'])
        self._input_device = merged['input_device']
        self._input_device_name = merged['input_device_name']
        self._prefer_wasapi = bool(merged['prefer_wasapi'])
        self._stall_timeout_sec = float(merged['stall_timeout_sec'])

    def _build_vad(self) -> sherpa_onnx.VoiceActivityDetector:
        vad_kwargs = {
            'model': str(self._vad_model_path),
            'threshold': float(self._vad_threshold),
            'min_silence_duration': float(self._vad_min_silence_duration),
            'min_speech_duration': float(self._vad_min_speech_duration),
            'window_size': int(self._vad_window_size),
            'max_speech_duration': float(self._vad_max_speech_duration),
        }
        try:
            sig = inspect.signature(sherpa_onnx.SileroVadModelConfig.__init__)
            params = sig.parameters
            if 'max_speech_duration' not in params and len(params) > 1:
                vad_kwargs.pop('max_speech_duration', None)
        except Exception:
            pass

        try:
            silero_cfg = sherpa_onnx.SileroVadModelConfig(**vad_kwargs)
        except TypeError:
            vad_kwargs.pop('max_speech_duration', None)
            silero_cfg = sherpa_onnx.SileroVadModelConfig(**vad_kwargs)

        vad_cfg = sherpa_onnx.VadModelConfig(
            silero_vad=silero_cfg,
            sample_rate=16000,
            num_threads=int(self._vad_num_threads),
            provider='cpu',
        )
        return sherpa_onnx.VoiceActivityDetector(
            vad_cfg,
            buffer_size_in_seconds=float(self._vad_buffer_size_seconds),
        )

    def create_vad(self) -> sherpa_onnx.VoiceActivityDetector:
        # Public helper returning a fresh VoiceActivityDetector instance.
        return self._build_vad()

    def initialize(self) -> bool:
        if self._initialized and self._recognizer is not None and self._vad is not None:
            return True

        t0 = time.perf_counter()
        try:
            for label, p in [
                ('model', self._model_path),
                ('tokens', self._tokens_path),
                ('vad_model', self._vad_model_path),
            ]:
                if not p.exists():
                    raise FileNotFoundError(f"STT asset '{label}' not found at {p}")

            self._vad = self._build_vad()
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
                model=str(self._model_path),
                tokens=str(self._tokens_path),
                num_threads=self._num_threads,
                provider='cpu',
            )

            if self._highpass_enabled:
                from scipy import signal
                self._sos = signal.butter(
                    2,
                    self._highpass_cutoff_hz,
                    btype='highpass',
                    fs=16000,
                    output='sos',
                )
            else:
                self._sos = None

            self._initialized = True
            init_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            logger.info(
                'STT engine initialized in %.1fms | model=%s vad=%s threads=%d (CPU-only, 0MB VRAM)',
                init_ms,
                self._model_path.name,
                self._vad_model_path.name,
                self._num_threads,
            )
            return True
        except Exception as exc:
            logger.exception('Failed to initialize STT engine: %s', exc)
            self.shutdown()
            return False

    def _preprocess_audio(self, audio_input: Any, sample_rate: int = 16000) -> np.ndarray:
        if isinstance(audio_input, (bytes, bytearray)):
            if len(audio_input) == 0:
                return np.array([], dtype=np.float32)
            audio = np.frombuffer(audio_input, dtype=np.int16).astype(np.float32) / 32768.0
        elif isinstance(audio_input, np.ndarray):
            if audio_input.size == 0:
                return np.array([], dtype=np.float32)
            if audio_input.ndim > 1:
                audio = np.mean(audio_input, axis=1)
            else:
                audio = audio_input
            if audio.dtype == np.int16:
                audio = audio.astype(np.float32) / 32768.0
            else:
                audio = audio.astype(np.float32)
        else:
            raise TypeError(f'Unsupported audio input type: {type(audio_input)}')

        if sample_rate != 16000 and len(audio) > 0:
            from scipy import signal
            gcd = math.gcd(16000, sample_rate)
            up = 16000 // gcd
            down = sample_rate // gcd
            audio = signal.resample_poly(audio, up, down).astype(np.float32)

        if self._gain != 1.0 and len(audio) > 0:
            audio = np.clip(audio * self._gain, -1.0, 1.0)

        if self._highpass_enabled and self._sos is not None and len(audio) > 0:
            from scipy import signal
            audio = signal.sosfilt(self._sos, audio).astype(np.float32)

        return np.ascontiguousarray(np.clip(audio, -1.0, 1.0), dtype=np.float32)

    @staticmethod
    def _reset_vad(vad: Any) -> None:
        if vad is None:
            return
        try:
            while not vad.empty():
                vad.pop()
            vad.reset()
            if hasattr(vad, 'clear'):
                vad.clear()
        except Exception as exc:
            logger.debug('VAD reset notice: %s', exc)

    def _decode_segment(self, samples: np.ndarray) -> Tuple[str, float]:
        pad_samples = int(self._pad_ms * 16.0)
        if pad_samples > 0:
            decode_audio = np.ascontiguousarray(
                np.pad(samples, (pad_samples, pad_samples), mode='constant'),
                dtype=np.float32,
            )
        else:
            decode_audio = np.ascontiguousarray(samples, dtype=np.float32)

        t0 = time.perf_counter()
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16000, decode_audio)
        self._recognizer.decode_stream(stream)
        infer_ms = (time.perf_counter() - t0) * 1000.0
        text = stream.result.text.strip()
        return text, infer_ms

    def transcribe_pcm(self, pcm_bytes: Any, sample_rate: int = 16000, language: Any = None) -> Dict[str, Any]:
        if not self._initialized:
            if not self.initialize():
                raise RuntimeError('STT engine could not be initialized')

        audio = self._preprocess_audio(pcm_bytes, sample_rate=sample_rate)
        audio_sec = round(len(audio) / 16000.0, 3)
        if len(audio) == 0:
            return {
                'text': '',
                'raw_text': '',
                'itn_text': '',
                'language': 'fa',
                'language_probability': 1.0,
                'segments': [],
                'duration': 0.0,
                'audio_sec': 0.0,
                'vad_ms': 0.0,
                'stt_ms': 0.0,
                'latency_ms': 0.0,
                'rtf': 0.0,
            }

        vad_ms = 0.0
        stt_ms = 0.0
        raw_parts: List[str] = []
        segments_list: List[Dict[str, Any]] = []

        with self._infer_lock:
            try:
                t_vad0 = time.perf_counter()
                window_size = int(self._vad_window_size)
                for i in range(0, len(audio), window_size):
                    chunk = audio[i:i + window_size]
                    if len(chunk) < window_size:
                        chunk = np.pad(chunk, (0, window_size - len(chunk)), mode='constant')
                    self._vad.accept_waveform(chunk)
                if hasattr(self._vad, 'flush'):
                    self._vad.flush()
                vad_ms = (time.perf_counter() - t_vad0) * 1000.0

                while not self._vad.empty():
                    seg = self._vad.front
                    seg_samples = np.array(seg.samples, dtype=np.float32)
                    seg_start_sample = seg.start
                    self._vad.pop()

                    seg_dur = round(len(seg_samples) / 16000.0, 3)
                    if seg_dur < 0.15:
                        continue

                    seg_start = round(seg_start_sample / 16000.0, 3)
                    seg_end = round(seg_start + seg_dur, 3)

                    seg_raw_text, seg_decode_ms = self._decode_segment(seg_samples)
                    stt_ms += seg_decode_ms
                    if seg_raw_text:
                        raw_parts.append(seg_raw_text)

                    seg_itn_text = (
                        apply_itn(seg_raw_text, persian_digits=self._itn_persian_digits).strip()
                        if self._enable_itn else seg_raw_text
                    )
                    segments_list.append({
                        'start': seg_start,
                        'end': seg_end,
                        'text': seg_itn_text,
                        'raw_text': seg_raw_text,
                        'duration': seg_dur,
                    })
            finally:
                self._reset_vad(self._vad)

        raw_text = ' '.join(p for p in raw_parts if p).strip()
        itn_text = (
            apply_itn(raw_text, persian_digits=self._itn_persian_digits).strip()
            if self._enable_itn else raw_text
        )
        rtf = round((stt_ms / 1000.0) / audio_sec, 4) if audio_sec > 0 else 0.0

        result = {
            'text': itn_text,
            'raw_text': raw_text,
            'itn_text': itn_text,
            'language': 'fa',
            'language_probability': 1.0,
            'segments': segments_list,
            'duration': round(audio_sec, 3),
            'audio_sec': round(audio_sec, 3),
            'vad_ms': round(vad_ms, 2),
            'stt_ms': round(stt_ms, 2),
            'latency_ms': round(vad_ms + stt_ms, 2),
            'rtf': rtf,
        }

        logger.info(
            'STT OK | audio_sec=%.2f | vad_ms=%.1f | stt_ms=%.1f | RTF=%.4f | segs=%d | raw_text=%s | itn_text=%s',
            audio_sec,
            vad_ms,
            stt_ms,
            rtf,
            len(segments_list),
            raw_text or '<empty>',
            itn_text or '<empty>',
        )
        return result

    def transcribe_file(self, audio_path: str, language: Any = None) -> Dict[str, Any]:
        path = Path(audio_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.exists():
            raise FileNotFoundError(f'Audio file not found: {path}')

        import soundfile as sf
        audio, sr = sf.read(str(path), dtype='float32')
        return self.transcribe_pcm(audio, sample_rate=sr, language=language)

    def is_ready(self) -> bool:
        return self._initialized and self._recognizer is not None and self._vad is not None

    def get_status(self) -> Dict[str, Any]:
        return {
            'initialized': self._initialized,
            'engine': 'shenava_sherpa_onnx',
            'provider': 'cpu',
            'num_threads': self._num_threads,
            'model_path': str(self._model_path),
            'tokens_path': str(self._tokens_path),
            'vad_model_path': str(self._vad_model_path),
            'enable_itn': self._enable_itn,
            'itn_persian_digits': self._itn_persian_digits,
        }

    def shutdown(self) -> None:
        with self._infer_lock:
            if self._vad is not None:
                try:
                    self._reset_vad(self._vad)
                except Exception:
                    pass
                self._vad = None
            if self._recognizer is not None:
                self._recognizer = None
            self._initialized = False
            logger.info('STT engine shut down')

    def close(self) -> None:
        self.shutdown()


if __name__ == '__main__':
    print('=' * 60)
    print('V.E.S.P.E.R. STT Engine (Shenava + Silero VAD) - Self Test')
    print('=' * 60)
    engine = STTEngine()
    ok = engine.initialize()
    assert ok, 'STTEngine initialization failed!'
    print('[TEST 1] Initialization: OK')

    # Test 1: 2 seconds of silence
    silent_pcm = np.zeros(32000, dtype=np.int16).tobytes()
    res_silence = engine.transcribe_pcm(silent_pcm, sample_rate=16000)
    assert res_silence['text'] == '', f"Expected empty text for silence, got: '{res_silence['text']}'"
    assert res_silence['stt_ms'] == 0.0, f"Expected stt_ms == 0.0 for silence, got: {res_silence['stt_ms']}"
    print(f"[TEST 1] Silence transcription: text='{res_silence['text']}' | vad_ms={res_silence['vad_ms']} | stt_ms={res_silence['stt_ms']} - OK")

    # Test 2: Real Persian speech file Voice/STT/test_data/sample_fa.wav
    sample_path = PROJECT_ROOT / 'Voice' / 'STT' / 'test_data' / 'sample_fa.wav'
    if sample_path.exists():
        res_sample = engine.transcribe_file(str(sample_path))
        print(f"[TEST 2] Persian audio: audio_sec={res_sample['audio_sec']} | vad_ms={res_sample['vad_ms']} | stt_ms={res_sample['stt_ms']} | rtf={res_sample['rtf']}")
        print(f"        raw_text: {res_sample['raw_text']}")
        print(f"        itn_text: {res_sample['itn_text']}")
        assert len(res_sample['text']) > 0, 'Expected non-empty transcription for sample_fa.wav'
        print('[TEST 2] Real speech sample transcription: OK')
    else:
        print('[TEST 2] sample_fa.wav not found, skipped')

    # Test 3: Call resolve_input_device('default')
    resolved_dev = resolve_input_device('default')
    print(f'[TEST 3] Resolved default input device: {resolved_dev}')

    engine.close()
    print('PHASE 2 SELF-TEST OK')
    print('=' * 60)
