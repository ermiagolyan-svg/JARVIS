'''C.E.L.E.S.T.E. Voice Pipeline Orchestrator - Stage 3.4 + Unified Observability.

Connects WakeWordEngine, STTEngine, DialogueManager, and TTSEngine into a
coherent state-machine-driven voice pipeline with procedural activation chime,
barge-in guard, VOICE-xxxxxx end-to-end turn correlation, and stage latency profiling.
'''
import math
import queue
import sys
import threading
import time
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import numpy as np
from scipy.signal import lfilter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Core.Logger.logger import (
    get_logger,
    get_turn_id,
    new_turn_id,
    setup_unified_logging,
    turn_context,
)

setup_unified_logging()
logger = get_logger('voice_orchestrator')

_INSTANCE_LOCK = threading.Lock()
_ACTIVE_INSTANCE: Optional['VoiceOrchestrator'] = None


class VoiceState(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    MUTED = auto()
    AWAITING_CONFIRMATION = auto()


class VoiceOrchestrator:
    '''Orchestrates the full voice pipeline as a finite state machine.

    State transitions::

        IDLE       -> LISTENING   (wake-word detected)
        LISTENING  -> THINKING    (speech captured, transcribing)
        THINKING   -> SPEAKING    (LLM response ready)
        SPEAKING   -> IDLE        (speech finished / barge-in)
        SPEAKING   -> MUTED       (user mutes during speech)
        IDLE       -> MUTED       (explicit mute command)
        LISTENING  -> AWAITING_CONFIRMATION (ambiguous intent)
        AWAITING_CONFIRMATION -> THINKING or IDLE (after confirm/deny)

    Audio flow::

        mic -> WakeWordEngine.ring_buffer -> STT.transcribe_pcm
             -> DialogueManager.chat() -> TTSEngine.speak()
    '''

    # Activation chime: two-tone procedural tone (C5 -> E5)
    CHIME_FREQS = [523.25, 659.25]       # C5, E5
    CHIME_DURATION = 0.18                 # seconds per tone
    CHIME_SR = 22050

    @classmethod
    def get_instance(cls) -> Optional['VoiceOrchestrator']:
        return _ACTIVE_INSTANCE

    def __init__(self, on_state_change=None):
        global _ACTIVE_INSTANCE
        self._state = VoiceState.IDLE
        self._lock = threading.Lock()
        self._speaking = False
        self._muted = False
        self._tts_echo_suppressed = False
        self._echo_lock = threading.Lock()
        self._confirmation_pending = None
        self._on_state_change = on_state_change or (lambda s: None)
        self._active_turn_id: Optional[str] = None
        self._wake_detected_at: Optional[float] = None
        self._on_hud_rms = None
        self._on_hud_message = None
        self._active_audio_source: str = 'mic'
        self._manual_lock = threading.Lock()
        self._manual_listening: bool = False
        self._manual_pipeline_active: bool = False
        self._manual_stop_event = threading.Event()
        self._manual_cancel_event = threading.Event()
        self._manual_session_id: int = 0
        self._manual_thread: Optional[threading.Thread] = None
        self._lumia_manual_queue: Optional[queue.Queue] = None
        # Component instances (lazy-init or injected)
        self.wake_word_engine = None
        self.stt_engine = None
        self.dialogue_manager = None
        self.tts_engine = None
        self._initialized = False
        with _INSTANCE_LOCK:
            if _ACTIVE_INSTANCE is None:
                _ACTIVE_INSTANCE = self

    def _resolve_dialogue_manager(self):
        if self.dialogue_manager is not None:
            return self.dialogue_manager
        try:
            from Network.API.rest_server import _get_dialogue_manager
            dm = _get_dialogue_manager()
            if dm is not None:
                self.dialogue_manager = dm
                return dm
        except Exception as exc:
            logger.debug('Could not borrow shared DialogueManager from rest_server: %s', exc)
        from Core.Conversation.dialogue_manager import DialogueManager
        self.dialogue_manager = DialogueManager()
        return self.dialogue_manager

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        '''Initialize all pipeline components.'''
        if self._initialized:
            return True
        try:
            from Voice.WakeWord.wakeword_engine import WakeWordEngine
            from Voice.STT.stt_engine import STTEngine
            from Core.Conversation.dialogue_manager import DialogueManager
            from Voice.TTS.tts_engine import TTSEngine

            if self.wake_word_engine is None:
                self.wake_word_engine = WakeWordEngine(
                    on_wake_word_detected=self._on_wake_word,
                    speech_recorded_callback=self._on_speech_captured,
                )
            if not self.wake_word_engine.initialize():
                logger.error('WakeWordEngine init failed')
                return False

            if self.stt_engine is None:
                self.stt_engine = STTEngine()
            if not self.stt_engine.initialize():
                logger.error('STT engine init failed')
                return False

            if self.dialogue_manager is None:
                self.dialogue_manager = DialogueManager()

            if self.tts_engine is None:
                self.tts_engine = TTSEngine.get_instance()
            if not self.tts_engine.initialize():
                logger.error('TTSEngine init failed')
                return False

            self._initialized = True
            logger.info('VoiceOrchestrator initialized (all components OK)')
            return True
        except Exception as exc:
            logger.exception('Orchestrator init error: %s', exc)
            return False

    def start(self):
        '''Start the pipeline (start wake-word listening).'''
        if not self._initialized:
            if not self.initialize():
                raise RuntimeError('Orchestrator not initialized')
        if self.wake_word_engine is not None:
            self.wake_word_engine.start()
        logger.info('Voice pipeline started - listening for wake word')

    def stop(self):
        '''Stop the pipeline.'''
        self._manual_cancel_event.set()
        self._manual_stop_event.set()
        if self.wake_word_engine is not None and hasattr(self.wake_word_engine, 'stop'):
            try:
                self.wake_word_engine.stop()
            except Exception as exc:
                logger.warning('WakeWordEngine stop warning: %s', exc)
        self.set_state(VoiceState.IDLE)
        logger.info('Voice pipeline stopped')

    def shutdown(self):
        '''Full shutdown of all components.'''
        self.stop()
        if self.stt_engine is not None and hasattr(self.stt_engine, 'shutdown'):
            try:
                self.stt_engine.shutdown()
            except Exception as exc:
                logger.warning('STTEngine shutdown warning: %s', exc)
        if self.tts_engine is not None and hasattr(self.tts_engine, 'shutdown'):
            try:
                self.tts_engine.shutdown()
            except Exception as exc:
                logger.warning('TTSEngine shutdown warning: %s', exc)
        logger.info('VoiceOrchestrator fully shut down')

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def get_state(self) -> VoiceState:
        with self._lock:
            return self._state

    def set_state(self, new_state: VoiceState):
        old = self._state
        if old != new_state:
            logger.info('Voice state: %s -> %s', old.name, new_state.name)
            with self._lock:
                self._state = new_state
                self._speaking = (new_state == VoiceState.SPEAKING)
            try:
                self._on_state_change(new_state)
            except Exception as exc:
                logger.warning('on_state_change callback error: %s', exc)

    def is_muted(self) -> bool:
        return self._muted

    def is_echo_suppressed(self) -> bool:
        with self._echo_lock:
            return self._tts_echo_suppressed

    def pause_listening_for_tts(self) -> None:
        with self._echo_lock:
            self._tts_echo_suppressed = True
            if self.wake_word_engine is not None and hasattr(self.wake_word_engine, 'set_muted'):
                try:
                    self.wake_word_engine.set_muted(True)
                except Exception as exc:
                    logger.warning('Failed to pause wake word engine for TTS: %s', exc)
        logger.debug('Echo suppression active: mic/network listening paused during TTS')

    def resume_listening_after_tts(self, tail_cooldown_s: float = 0.25) -> None:
        if tail_cooldown_s > 0 and not self._muted:
            time.sleep(tail_cooldown_s)
        with self._echo_lock:
            self._tts_echo_suppressed = False
            if not self._muted and self.wake_word_engine is not None and hasattr(self.wake_word_engine, 'set_muted'):
                try:
                    self.wake_word_engine.set_muted(False)
                except Exception as exc:
                    logger.warning('Failed to resume wake word engine after TTS: %s', exc)
        logger.debug('Echo suppression released: listening resumed (muted=%s)', self._muted)

    def set_muted(self, muted: bool) -> None:
        if muted:
            self.mute()
        else:
            self.unmute()

    def set_audio_source(self, source: str) -> None:
        self._active_audio_source = source if source in ('mic', 'network') else 'mic'
        if self.wake_word_engine is not None and hasattr(self.wake_word_engine, 'set_audio_source'):
            try:
                self.wake_word_engine.set_audio_source(source)
                logger.info('VoiceOrchestrator audio source switched to: %s', source)
            except Exception as exc:
                logger.warning('Failed to switch audio source to %s: %s', source, exc)

    def ingest_lumia_audio_chunk(self, pcm_bytes: bytes) -> None:
        if self._muted or self._tts_echo_suppressed or not pcm_bytes:
            return
        q = self._lumia_manual_queue
        if self._manual_listening and q is not None:
            try:
                q.put_nowait(pcm_bytes)
            except queue.Full:
                pass
            return
        if self.wake_word_engine is not None and hasattr(self.wake_word_engine, 'feed_audio'):
            self.wake_word_engine.feed_audio(pcm_bytes)

    # ------------------------------------------------------------------
    # Barge-in guard
    # ------------------------------------------------------------------

    def barge_in(self):
        '''Interrupt current speech if user speaks during SPEAKING.'''
        with self._echo_lock:
            self._tts_echo_suppressed = False
            if not self._muted and self.wake_word_engine is not None and hasattr(self.wake_word_engine, 'set_muted'):
                try:
                    self.wake_word_engine.set_muted(False)
                except Exception as exc:
                    logger.warning('Failed to unmute wake word engine on barge-in: %s', exc)

        if self.get_state() == VoiceState.SPEAKING and not self._muted:
            logger.info('BARGE-IN detected - interrupting TTS playback')
            if self.tts_engine is not None and hasattr(self.tts_engine, 'stop_speaking'):
                self.tts_engine.stop_speaking()
            self.set_state(VoiceState.LISTENING)

    # ------------------------------------------------------------------
    # Procedural activation chime (C5 -> E5, metallic style)
    # ------------------------------------------------------------------

    def play_activation_chime(self):
        '''Play a procedural two-tone activation chime.'''
        sr = self.CHIME_SR
        duration = self.CHIME_DURATION
        t = np.linspace(0, duration, int(sr * duration), dtype=np.float32)
        tone1 = np.sin(2 * np.pi * self.CHIME_FREQS[0] * t).astype(np.float32)
        tone2 = np.sin(2 * np.pi * self.CHIME_FREQS[1] * t).astype(np.float32)

        # Crossfade: fade out tone1, fade in tone2
        fade_len = int(sr * 0.04)
        fade_in = np.linspace(0, 1, fade_len)
        fade_out = np.linspace(1, 0, fade_len)

        tone1[:fade_len] *= fade_out
        tone2[-fade_len:] *= fade_in

        chime = np.clip(tone1 + tone2, -1.0, 1.0)

        try:
            from Voice.AudioDSP.jarvis_dsp import JarvisAudioDSP
            dsp = JarvisAudioDSP(pitch_semitones=-3.0, sample_rate=self.CHIME_SR)
            chime = dsp.process(chime)
        except Exception as exc:
            logger.warning('Activation chime DSP bypass: %s', exc)

        try:
            import sounddevice as sd
            sd.play(chime, samplerate=sr, blocking=False)
            logger.debug('Activation chime played (%d samples @ %d Hz)', len(chime), sr)
        except ImportError:
            logger.warning('sounddevice not available - activation chime skipped')
        except Exception as exc:
            logger.warning('Activation chime playback error: %s', exc)

    # ------------------------------------------------------------------
    # Internal pipeline helpers & callbacks
    # ------------------------------------------------------------------

    def _route_actions(self, response_text: str) -> None:
        '''Log any residual unparsed <action> tags (DialogueManager executes primary actions).'''
        if not response_text or '<action>' not in response_text:
            return
        try:
            from Core.Conversation.dialogue_manager import parse_actions
            actions, _ = parse_actions(response_text)
            if actions:
                logger.info('Residual voice actions detected: %d', len(actions))
        except Exception as exc:
            logger.warning('Action inspection warning: %s', exc)

    def _wait_for_speech(self, timeout_s: float = 45.0) -> float:
        '''Wait until TTSEngine finishes speaking or barge-in/mute occurs; returns wait ms.'''
        t0 = time.perf_counter()
        if self.tts_engine is None:
            return 0.0
        # Give async speak worker a brief window to enter speaking=True
        time.sleep(0.08)
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            if self._muted or self.get_state() != VoiceState.SPEAKING:
                break
            is_speaking = False
            if hasattr(self.tts_engine, 'get_status'):
                try:
                    is_speaking = bool(self.tts_engine.get_status().get('speaking', False))
                except Exception:
                    is_speaking = False
            elif hasattr(self.tts_engine, '_speaking'):
                is_speaking = bool(self.tts_engine._speaking)
            if not is_speaking:
                break
            time.sleep(0.05)
        return round((time.perf_counter() - t0) * 1000.0, 1)

    def _on_wake_word(self, ww_name):
        if self._muted:
            return
        tid = new_turn_id('VOICE')
        self._active_turn_id = tid
        self._wake_detected_at = time.perf_counter()
        with turn_context(tid):
            logger.info('Turn start [VOICE] | wake_word="%s" detected', ww_name)
            self.play_activation_chime()
            self.set_state(VoiceState.LISTENING)

    def _on_speech_captured(self, pcm_bytes):
        tid = self._active_turn_id or new_turn_id('VOICE')
        self._active_turn_id = None
        wake_to_capture_ms = (
            round((time.perf_counter() - self._wake_detected_at) * 1000.0, 1)
            if self._wake_detected_at is not None else 0.0
        )
        self._wake_detected_at = None

        with turn_context(tid):
            t_turn0 = time.perf_counter()
            audio_s = round(len(pcm_bytes) / 32000.0, 2) if pcm_bytes else 0.0
            logger.info(
                'Speech captured [VOICE] | bytes=%d (%.2fs @ 16kHz) | capture_window=%.1fms',
                len(pcm_bytes or b''), audio_s, wake_to_capture_ms,
            )
            self.set_state(VoiceState.THINKING)
            stt_ms = 0.0
            llm_ms = 0.0
            tts_wait_ms = 0.0
            try:
                if self.stt_engine is None:
                    raise RuntimeError('STTEngine not initialized on VoiceOrchestrator')

                t_stt = time.perf_counter()
                result = self.stt_engine.transcribe_pcm(pcm_bytes, sample_rate=16000, language='fa')
                stt_ms = round((time.perf_counter() - t_stt) * 1000.0, 1)
                text = (result.get('text') or '').strip()
                vad_ms = float(result.get('vad_ms', 0.0))
                stt_infer_ms = float(result.get('stt_ms', stt_ms))
                stt_rtf = float(result.get('rtf', 0.0))
                raw_text = (result.get('raw_text') or text).strip()
                itn_text = (result.get('itn_text') or text).strip()

                if not text:
                    logger.info('Voice turn ignored (empty STT transcript after %.1fms | vad_ms=%.1f stt_ms=%.1f)', stt_ms, vad_ms, stt_infer_ms)
                    return

                # Broadcast transcribed user prompt to HUD immediately
                self._emit_hud_message({'role': 'user', 'text': text, 'turn_id': tid, 'source': 'voice'})

                dm = self._resolve_dialogue_manager()
                t_llm = time.perf_counter()
                response_text = dm.chat(text)
                llm_ms = round((time.perf_counter() - t_llm) * 1000.0, 1)

                self._route_actions(response_text)
                self._emit_hud_message({'role': 'assistant', 'text': response_text, 'turn_id': tid, 'source': 'voice'})

                if self._muted:
                    logger.info('Voice turn skipping TTS (muted) | stt=%.1fms llm=%.1fms', stt_ms, llm_ms)
                    return

                raw_tts = getattr(dm, 'last_raw_tts_response', None) or response_text
                if raw_tts and self.tts_engine is not None:
                    self.set_state(VoiceState.SPEAKING)
                    self.pause_listening_for_tts()
                    try:
                        try:
                            self.tts_engine.speak(raw_tts, blocking=False, turn_id=tid)
                        except TypeError:
                            self.tts_engine.speak(raw_tts, blocking=False)
                        tts_wait_ms = self._wait_for_speech()
                    finally:
                        self.resume_listening_after_tts(tail_cooldown_s=0.25)

                total_ms = round((time.perf_counter() - t_turn0) * 1000.0, 1)
                logger.info(
                    'Voice Turn Complete | vad_ms=%.1fms stt=%.1fms rtf=%.4f llm=%.1fms tts_wait=%.1fms total=%.1fms | user_chars=%d reply_chars=%d',
                    vad_ms, stt_ms, stt_rtf, llm_ms, tts_wait_ms, total_ms, len(text), len(response_text or ''),
                )
            except Exception as exc:
                total_ms = round((time.perf_counter() - t_turn0) * 1000.0, 1)
                logger.exception(
                    'Voice pipeline error after %.1fms (stt=%.1fms llm=%.1fms): %s',
                    total_ms, stt_ms, llm_ms, exc,
                )
            finally:
                if not self._muted and self.get_state() in (VoiceState.THINKING, VoiceState.SPEAKING):
                    self.set_state(VoiceState.IDLE)

    # ------------------------------------------------------------------
    # Manual Mic -> Shenava STT Pipeline (Independent of WakeWordEngine)
    # ------------------------------------------------------------------

    def set_hud_callbacks(self, on_state_change=None, on_rms=None, on_message=None) -> None:
        '''Bind HUD telemetry callbacks (used by JarvisWebSocketManager).'''
        if on_state_change is not None:
            self._on_state_change = on_state_change
        if on_rms is not None:
            self._on_hud_rms = on_rms
        if on_message is not None:
            self._on_hud_message = on_message

    def _emit_hud_rms(self, rms: float) -> None:
        cb = self._on_hud_rms
        if cb is None:
            return
        try:
            cb(max(0.0, min(1.0, float(rms))))
        except Exception as exc:
            logger.debug('on_hud_rms callback error: %s', exc)

    def _emit_hud_message(self, payload: dict) -> None:
        cb = self._on_hud_message
        if cb is None:
            return
        try:
            cb(dict(payload))
        except Exception as exc:
            logger.debug('on_hud_message callback error: %s', exc)

    def _resolve_stt_engine(self):
        if self.stt_engine is None:
            from Voice.STT.stt_engine import STTEngine
            self.stt_engine = STTEngine()
        if not self.stt_engine.is_ready():
            if not self.stt_engine.initialize():
                raise RuntimeError('STTEngine initialization failed')
        return self.stt_engine

    def _resolve_tts_engine(self):
        if self.tts_engine is not None:
            return self.tts_engine
        try:
            from Voice.TTS.tts_engine import TTSEngine
            self.tts_engine = TTSEngine.get_instance()
        except Exception as exc:
            logger.warning('Could not resolve TTSEngine in VoiceOrchestrator: %s', exc)
        return self.tts_engine

    def is_manual_listening(self) -> bool:
        with self._manual_lock:
            return bool(self._manual_listening)

    def start_manual_listening(self, source: str = 'desktop', manual: bool = True) -> dict:
        '''Start an immediate manual mic capture -> Shenava STT -> Gemma-4 -> TTS turn.'''
        with self._manual_lock:
            if self._muted:
                logger.info('Manual wake_trigger rejected (muted) | source=%s', source)
                return {'accepted': False, 'reason': 'muted', 'state': self._state.name}

            curr_state = self.get_state()
            if self._manual_listening or curr_state == VoiceState.LISTENING:
                logger.info('Manual wake_trigger ignored (already listening) | source=%s', source)
                return {'accepted': False, 'reason': 'already_listening', 'state': curr_state.name}

            if curr_state == VoiceState.THINKING or (self._manual_pipeline_active and curr_state != VoiceState.SPEAKING):
                logger.info('Manual wake_trigger ignored (pipeline busy: %s) | source=%s', curr_state.name, source)
                return {'accepted': False, 'reason': 'busy_thinking', 'state': curr_state.name}

            was_speaking = (curr_state == VoiceState.SPEAKING)
            if was_speaking:
                logger.info('Manual wake_trigger barge-in during SPEAKING | source=%s', source)
                tts = self._resolve_tts_engine()
                if tts is not None and hasattr(tts, 'stop_speaking'):
                    try:
                        tts.stop_speaking()
                    except Exception as exc:
                        logger.warning('Failed to stop TTS during manual barge-in: %s', exc)
                with self._echo_lock:
                    self._tts_echo_suppressed = False

            tid = new_turn_id('MAN')
            self._manual_session_id += 1
            session_id = self._manual_session_id
            self._manual_listening = True
            self._manual_pipeline_active = True
            self._manual_stop_event.clear()
            self._manual_cancel_event.clear()

        self._emit_hud_rms(0.0)
        self.set_state(VoiceState.LISTENING)

        t = threading.Thread(
            target=self._manual_voice_session,
            args=(session_id, tid, source, was_speaking),
            daemon=True,
            name=f'ManualVoice-{tid}',
        )
        self._manual_thread = t
        t.start()
        return {'accepted': True, 'turn_id': tid, 'state': 'LISTENING'}

    def stop_manual_listening(self, source: str = 'desktop') -> dict:
        '''Signal an active manual mic capture to stop early and process captured audio.'''
        with self._manual_lock:
            if not self._manual_listening:
                return {'stopped': False, 'reason': 'not_listening', 'state': self.get_state().name}
            self._manual_stop_event.set()
            logger.info('Manual stop_listening signaled | source=%s', source)
            return {'stopped': True, 'state': self.get_state().name}

    def _capture_manual_audio(self, session_id: int, source: str):
        '''Capture mono 16kHz float32 audio with Silero VAD end-of-speech + live RMS streaming.'''
        from Voice.STT.stt_engine import STTEngine, open_resilient_stream, resolve_input_device
        from scipy import signal as sp_signal

        stt = self._resolve_stt_engine()
        target_sr = 16000
        window_size = int(getattr(stt, '_vad_window_size', 512) or 512)
        min_silence_sec = float(getattr(stt, '_vad_min_silence_duration', 0.65) or 0.65)
        max_record_sec = float(getattr(stt, '_vad_max_speech_duration', 15.0) or 15.0)
        stall_timeout_sec = float(getattr(stt, '_stall_timeout_sec', 3.0) or 3.0)
        no_speech_timeout_sec = 7.0

        vad = stt.create_vad()
        audio_q: queue.Queue = queue.Queue(maxsize=256)
        use_network = (self._active_audio_source == 'network' and source != 'desktop')
        stream = None
        actual_sr = target_sr
        resolved_idx = None

        def _sd_callback(indata, frames, time_info, status_flags):
            if status_flags:
                logger.debug('sounddevice manual capture status: %s', status_flags)
            try:
                audio_q.put_nowait(indata.copy())
            except queue.Full:
                pass

        if use_network:
            self._lumia_manual_queue = audio_q
            logger.info('Manual capture listening on network audio stream (16kHz PCM)')
        else:
            dev_idx = resolve_input_device(
                getattr(stt, '_input_device', 'default'),
                getattr(stt, '_input_device_name', ''),
                bool(getattr(stt, '_prefer_wasapi', True)),
            )
            stream, actual_sr, resolved_idx = open_resilient_stream(
                dev_idx,
                target_sr=target_sr,
                callback=_sd_callback,
            )
            if not getattr(stream, 'active', False):
                stream.start()
            logger.info(
                'Manual mic stream opened | device=%s @ %d Hz | max=%.1fs silence=%.2fs',
                resolved_idx, actual_sr, max_record_sec, min_silence_sec,
            )

        captured_chunks = []
        vad_buf = np.zeros(0, dtype=np.float32)
        start_t = time.perf_counter()
        last_frame_t = start_t
        last_rms_emit_t = 0.0
        smoothed_rms = 0.0
        speech_detected = False
        energy_speech_frames = 0
        energy_speech_seen = False
        last_speech_t = start_t
        end_reason = 'completed'

        try:
            while True:
                if self._manual_cancel_event.is_set() or self._muted or session_id != self._manual_session_id:
                    end_reason = 'cancelled'
                    break
                if self._manual_stop_event.is_set():
                    end_reason = 'manual_stop'
                    break

                now = time.perf_counter()
                elapsed = now - start_t
                if elapsed >= max_record_sec:
                    end_reason = 'max_duration'
                    break

                try:
                    raw_item = audio_q.get(timeout=0.05)
                except queue.Empty:
                    now = time.perf_counter()
                    if (now - last_frame_t) >= stall_timeout_sec:
                        logger.warning('Manual capture stalled (no audio frames for %.1fs)', stall_timeout_sec)
                        end_reason = 'stall_timeout'
                        break
                    if (not speech_detected) and (not energy_speech_seen) and (now - start_t) >= no_speech_timeout_sec:
                        end_reason = 'no_speech_timeout'
                        break
                    continue

                now = time.perf_counter()
                last_frame_t = now

                if isinstance(raw_item, (bytes, bytearray)):
                    if not raw_item:
                        continue
                    chunk_mono = np.frombuffer(raw_item, dtype=np.int16).astype(np.float32) / 32768.0
                    item_sr = 16000
                else:
                    arr = np.asarray(raw_item, dtype=np.float32)
                    if arr.size == 0:
                        continue
                    if arr.ndim > 1:
                        if arr.shape[1] > 1:
                            chunk_mono = np.mean(arr, axis=1, dtype=np.float32)
                        else:
                            chunk_mono = arr[:, 0].astype(np.float32)
                    else:
                        chunk_mono = arr.reshape(-1)
                    item_sr = actual_sr

                # Compute normalized RMS (0.0 .. 1.0) for smooth 3D HUD globe reactivity
                raw_rms = float(np.sqrt(np.mean(chunk_mono * chunk_mono)))
                norm_rms = min(1.0, max(0.0, raw_rms * 4.2))
                smoothed_rms = (0.50 * smoothed_rms) + (0.50 * norm_rms)
                if (now - last_rms_emit_t) >= 0.05:
                    self._emit_hud_rms(smoothed_rms)
                    last_rms_emit_t = now

                if raw_rms >= 0.015:
                    energy_speech_frames += 1
                    if energy_speech_frames >= 3:
                        energy_speech_seen = True
                        last_speech_t = now
                else:
                    energy_speech_frames = 0

                if item_sr != target_sr and len(chunk_mono) > 0:
                    gcd = math.gcd(target_sr, int(item_sr))
                    up = target_sr // gcd
                    down = int(item_sr) // gcd
                    chunk_16k = sp_signal.resample_poly(chunk_mono, up, down).astype(np.float32)
                else:
                    chunk_16k = np.ascontiguousarray(chunk_mono, dtype=np.float32)

                if chunk_16k.size == 0:
                    continue

                captured_chunks.append(chunk_16k)
                vad_buf = np.concatenate((vad_buf, chunk_16k)) if vad_buf.size > 0 else chunk_16k

                vad_finished = False
                while vad_buf.size >= window_size:
                    win = np.ascontiguousarray(vad_buf[:window_size], dtype=np.float32)
                    vad_buf = vad_buf[window_size:]
                    vad.accept_waveform(win)
                    if hasattr(vad, 'is_speech_detected'):
                        try:
                            if vad.is_speech_detected():
                                speech_detected = True
                                last_speech_t = now
                        except Exception:
                            pass
                    if not vad.empty():
                        speech_detected = True
                        vad_finished = True
                        break

                if vad_finished:
                    end_reason = 'vad_silence'
                    break

                if speech_detected and (now - last_speech_t) >= max(min_silence_sec + 0.65, 1.4):
                    end_reason = 'silence_fallback'
                    break

                if (not speech_detected) and (not energy_speech_seen) and (now - start_t) >= no_speech_timeout_sec:
                    end_reason = 'no_speech_timeout'
                    break
        finally:
            self._lumia_manual_queue = None
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            try:
                STTEngine._reset_vad(vad)
            except Exception:
                pass
            with self._manual_lock:
                if session_id == self._manual_session_id:
                    self._manual_listening = False
            self._emit_hud_rms(0.0)

        audio_16k = np.concatenate(captured_chunks) if captured_chunks else np.zeros(0, dtype=np.float32)
        dur_s = round(len(audio_16k) / float(target_sr), 2)
        logger.info(
            'Manual capture finished | reason=%s | audio=%.2fs | speech_vad=%s | speech_energy=%s',
            end_reason, dur_s, speech_detected, energy_speech_seen,
        )
        return audio_16k, end_reason, (speech_detected or energy_speech_seen)

    def _manual_voice_session(self, session_id: int, tid: str, source: str, was_speaking: bool) -> None:
        with turn_context(tid):
            t_turn0 = time.perf_counter()
            ww_paused_by_manual = False
            try:
                if was_speaking:
                    time.sleep(0.12)

                if self.wake_word_engine is not None and hasattr(self.wake_word_engine, 'set_muted'):
                    try:
                        self.wake_word_engine.set_muted(True)
                        ww_paused_by_manual = True
                    except Exception as exc:
                        logger.debug('Could not temporarily pause WakeWordEngine: %s', exc)

                audio_16k, end_reason, had_speech = self._capture_manual_audio(session_id, source)

                if end_reason == 'cancelled' or self._muted or session_id != self._manual_session_id:
                    logger.info('Manual voice turn aborted (reason=%s, muted=%s)', end_reason, self._muted)
                    return

                min_samples = int(16000 * 0.25)
                if audio_16k.size < min_samples or (end_reason in ('no_speech_timeout', 'stall_timeout') and not had_speech):
                    logger.info('Manual voice turn ignored (no usable speech | reason=%s samples=%d)', end_reason, audio_16k.size)
                    return

                if end_reason == 'manual_stop' and not had_speech and audio_16k.size < int(16000 * 0.6):
                    logger.info('Manual stop_listening received before speech started; returning to IDLE')
                    return

                # Transition to THINKING and decode with Shenava STT
                self.set_state(VoiceState.THINKING)
                stt = self._resolve_stt_engine()

                # Append 250ms trailing silence so Silero VAD inside transcribe_pcm cleanly flushes final syllables
                padded_audio = np.concatenate((audio_16k, np.zeros(4000, dtype=np.float32)))
                t_stt = time.perf_counter()
                stt_res = stt.transcribe_pcm(padded_audio, sample_rate=16000, language='fa')
                stt_ms = round((time.perf_counter() - t_stt) * 1000.0, 1)
                user_text = (stt_res.get('text') or '').strip()

                if not user_text:
                    logger.info('Manual voice turn ignored (empty Shenava STT transcript after %.1fms)', stt_ms)
                    return

                # Broadcast transcribed user prompt to HUD immediately
                self._emit_hud_message({'role': 'user', 'text': user_text, 'turn_id': tid, 'source': 'voice'})
                logger.info('Manual STT transcript [%.1fms]: %s', stt_ms, user_text[:140])

                if self._muted or session_id != self._manual_session_id:
                    return

                # Gemma-4 DialogueManager (persists user & assistant turns in SQLite)
                dm = self._resolve_dialogue_manager()
                t_llm = time.perf_counter()
                response_text = (dm.chat(user_text) or '').strip()
                llm_ms = round((time.perf_counter() - t_llm) * 1000.0, 1)

                if not response_text:
                    response_text = 'متأسفانه پاسخی از هسته پردازشی دریافت نشد مهندس، لطفاً دوباره بگو.'
                    raw_tts = ''
                else:
                    raw_tts = (getattr(dm, 'last_raw_tts_response', None) or response_text).strip()

                self._route_actions(response_text)
                self._emit_hud_message({'role': 'assistant', 'text': response_text, 'turn_id': tid, 'source': 'voice'})

                if self._muted or session_id != self._manual_session_id:
                    logger.info('Manual turn skipping TTS (muted/superseded) | stt=%.1fms llm=%.1fms', stt_ms, llm_ms)
                    return

                tts = self._resolve_tts_engine()
                tts_wait_ms = 0.0
                if raw_tts and tts is not None:
                    self.set_state(VoiceState.SPEAKING)
                    self.pause_listening_for_tts()
                    try:
                        try:
                            tts.speak(raw_tts, blocking=False, turn_id=tid)
                        except TypeError:
                            tts.speak(raw_tts, blocking=False)
                        tts_wait_ms = self._wait_for_speech()
                    finally:
                        self.resume_listening_after_tts(tail_cooldown_s=0.25)

                total_ms = round((time.perf_counter() - t_turn0) * 1000.0, 1)
                logger.info(
                    'Manual Voice Turn Complete | stt=%.1fms llm=%.1fms tts_wait=%.1fms total=%.1fms | user_chars=%d reply_chars=%d',
                    stt_ms, llm_ms, tts_wait_ms, total_ms, len(user_text), len(response_text),
                )
            except Exception as exc:
                logger.exception('Manual voice pipeline error: %s', exc)
                self._emit_hud_message({
                    'role': 'assistant',
                    'text': f'خطا در پردازش صوتی وسپر: {exc}',
                    'turn_id': tid,
                    'source': 'voice',
                })
            finally:
                is_current = False
                with self._manual_lock:
                    if session_id == self._manual_session_id:
                        self._manual_listening = False
                        self._manual_pipeline_active = False
                        is_current = True

                if is_current:
                    self._emit_hud_rms(0.0)
                    if ww_paused_by_manual and not self._muted and not self.is_echo_suppressed():
                        if self.wake_word_engine is not None and hasattr(self.wake_word_engine, 'set_muted'):
                            try:
                                self.wake_word_engine.set_muted(False)
                            except Exception:
                                pass
                    if self._muted:
                        self.set_state(VoiceState.MUTED)
                    elif self.get_state() in (VoiceState.LISTENING, VoiceState.THINKING, VoiceState.SPEAKING):
                        self.set_state(VoiceState.IDLE)

    # ------------------------------------------------------------------
    # Public convenience methods
    # ------------------------------------------------------------------

    def mute(self):
        '''Mute the pipeline.'''
        self._muted = True
        self._manual_cancel_event.set()
        self._manual_stop_event.set()
        if self.wake_word_engine and hasattr(self.wake_word_engine, 'set_muted'):
            self.wake_word_engine.set_muted(True)
        if self.tts_engine and hasattr(self.tts_engine, 'stop_speaking'):
            self.tts_engine.stop_speaking()
        self.set_state(VoiceState.MUTED)

    def unmute(self):
        '''Unmute the pipeline.'''
        self._muted = False
        with self._echo_lock:
            self._tts_echo_suppressed = False
        if self.wake_word_engine and hasattr(self.wake_word_engine, 'set_muted'):
            self.wake_word_engine.set_muted(False)
        self.set_state(VoiceState.IDLE)

    def apply_voice_config(
        self,
        pitch_semitones=None,
        speech_rate=None,
        metallic_mix=None,
        reverb_decay=None,
        muted=None,
        audio_source=None,
        wake_threshold=None,
        tts_config=None,
    ):
        '''Apply voice configuration changes live to the active pipeline.'''
        from Voice.AudioDSP.jarvis_dsp import JarvisAudioDSP

        JarvisAudioDSP.set_global_config(
            pitch_semitones=pitch_semitones,
            metallic_mix=metallic_mix,
            reverb_decay=reverb_decay,
            speech_rate=speech_rate,
        )

        if self.tts_engine and hasattr(self.tts_engine, '_dsp') and self.tts_engine._dsp is not None:
            try:
                self.tts_engine._dsp.update_config(
                    pitch_semitones=pitch_semitones,
                    metallic_mix=metallic_mix,
                    reverb_decay=reverb_decay,
                    speech_rate=speech_rate,
                )
            except Exception as exc:
                logger.warning('Failed to update cached TTSEngine DSP config: %s', exc)

        if tts_config and self.tts_engine and hasattr(self.tts_engine, 'update_config'):
            try:
                self.tts_engine.update_config(tts_config)
            except Exception as exc:
                logger.warning('Failed to push tts_config to TTSEngine: %s', exc)

        if muted is not None:
            if muted:
                self.mute()
            else:
                self.unmute()

        if self.wake_word_engine is not None:
            if audio_source is not None:
                try:
                    self.wake_word_engine.set_audio_source(audio_source)
                except Exception as exc:
                    logger.warning('Failed to set wake_word audio_source=%s: %s', audio_source, exc)
            if wake_threshold is not None and hasattr(self.wake_word_engine, 'set_wake_threshold'):
                try:
                    self.wake_word_engine.set_wake_threshold(float(wake_threshold))
                except Exception as exc:
                    logger.warning('Failed to set wake_threshold=%s: %s', wake_threshold, exc)

    def get_status(self) -> dict:
        '''Return pipeline status snapshot.'''
        return {
            'state': self._state.name,
            'muted': self._muted,
            'echo_suppressed': self._tts_echo_suppressed,
            'speaking': self._speaking,
            'initialized': self._initialized,
            'stt_status': self.stt_engine.get_status() if (self.stt_engine and hasattr(self.stt_engine, 'get_status')) else None,
            'tts_status': self.tts_engine.get_status() if self.tts_engine else None,
        }


# Self-test block

if __name__ == '__main__':
    print('=' * 60)
    print('V.E.S.P.E.R. VoiceOrchestrator - Self Test (Stage 3.4)')
    print('=' * 60)

    orch = VoiceOrchestrator()
    assert orch.get_state() == VoiceState.IDLE
    print('[TEST] Default state is IDLE: OK')

    orch.set_state(VoiceState.LISTENING)
    assert orch.get_state() == VoiceState.LISTENING
    orch.set_state(VoiceState.THINKING)
    assert orch.get_state() == VoiceState.THINKING
    orch.set_state(VoiceState.SPEAKING)
    assert orch.get_state() == VoiceState.SPEAKING
    orch.set_state(VoiceState.MUTED)
    assert orch.get_state() == VoiceState.MUTED
    orch.set_state(VoiceState.AWAITING_CONFIRMATION)
    assert orch.get_state() == VoiceState.AWAITING_CONFIRMATION
    orch.set_state(VoiceState.IDLE)
    print('[TEST] State machine transitions: OK')

    orch.mute()
    assert orch.is_muted() is True
    orch.unmute()
    assert orch.is_muted() is False
    print('[TEST] Mute/unmute: OK')

    assert orch.is_echo_suppressed() is False
    orch.pause_listening_for_tts()
    assert orch.is_echo_suppressed() is True
    assert orch.is_muted() is False

    class DummyWakeWord:
        def __init__(self):
            self.fed = False
        def feed_audio(self, chunk):
            self.fed = True
        def set_muted(self, muted):
            pass

    dummy_ww = DummyWakeWord()
    orch.wake_word_engine = dummy_ww
    orch.ingest_lumia_audio_chunk(b'\x00' * 320)
    assert dummy_ww.fed is False, 'Expected audio chunk to be blocked while echo suppressed'

    orch.resume_listening_after_tts(tail_cooldown_s=0.0)
    assert orch.is_echo_suppressed() is False
    assert orch.is_muted() is False

    orch.ingest_lumia_audio_chunk(b'\x00' * 320)
    assert dummy_ww.fed is True, 'Expected audio chunk to be ingested after echo suppression released'
    orch.wake_word_engine = None
    print('[TEST] Echo suppression toggle & Lumia gate: OK')

    orch.barge_in()
    print('[TEST] Barge-in (not speaking): OK')

    status = orch.get_status()
    assert 'state' in status
    assert 'muted' in status
    assert 'echo_suppressed' in status
    assert status['echo_suppressed'] is False
    assert isinstance(status['muted'], bool)
    print(f'[TEST] Status snapshot: {status["state"]}, muted={status["muted"]}, echo_suppressed={status["echo_suppressed"]} - OK')

    sr = orch.CHIME_SR
    duration = orch.CHIME_DURATION
    t = np.linspace(0, duration, int(sr * duration), dtype=np.float32)
    tone1 = np.sin(2 * np.pi * orch.CHIME_FREQS[0] * t).astype(np.float32)
    tone2 = np.sin(2 * np.pi * orch.CHIME_FREQS[1] * t).astype(np.float32)
    chime = np.clip(tone1 + tone2, -1.0, 1.0)
    assert len(chime) > 0
    assert np.max(np.abs(chime)) <= 1.0
    print(f'[TEST] Chime: {len(chime)} samples, peak={np.max(np.abs(chime)):.4f} - OK')

    from Voice.AudioDSP.jarvis_dsp import JarvisAudioDSP
    dsp = JarvisAudioDSP(pitch_semitones=-3.0)
    processed = dsp.process(chime)
    assert len(processed) > 0
    assert np.max(np.abs(processed)) <= 1.0
    print(f'[TEST] DSP on chime: {len(processed)} samples, peak={np.max(np.abs(processed)):.4f} - OK')

    from Core.Conversation.dialogue_manager import parse_actions
    test_text = 'سلام JARVIS <action>{"type": "run_command", "params": {"command": "ls"}}</action>'
    actions, remaining = parse_actions(test_text)
    assert len(actions) == 1
    assert actions[0]['type'] == 'run_command'
    print(f'[TEST] Action parsing: {len(actions)} action(s) - OK')

    print('=' * 60)
    print('STAGE 3.4 OK')
    print('=' * 60)