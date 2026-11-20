'''C.E.L.E.S.T.E. Voice Pipeline Orchestrator - Stage 3.4 + Unified Observability.

Connects WakeWordEngine, STTEngine, DialogueManager, and TTSEngine into a
coherent state-machine-driven voice pipeline with procedural activation chime,
barge-in guard, VOICE-xxxxxx end-to-end turn correlation, and stage latency profiling.
'''
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
        self._confirmation_pending = None
        self._on_state_change = on_state_change or (lambda s: None)
        self._active_turn_id: Optional[str] = None
        self._wake_detected_at: Optional[float] = None
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
                self.stt_engine = STTEngine(model_size='small', device='cpu', compute_type='int8')
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

    def set_muted(self, muted: bool) -> None:
        if muted:
            self.mute()
        else:
            self.unmute()

    def set_audio_source(self, source: str) -> None:
        if self.wake_word_engine is not None and hasattr(self.wake_word_engine, 'set_audio_source'):
            try:
                self.wake_word_engine.set_audio_source(source)
                logger.info('VoiceOrchestrator audio source switched to: %s', source)
            except Exception as exc:
                logger.warning('Failed to switch audio source to %s: %s', source, exc)

    def ingest_lumia_audio_chunk(self, pcm_bytes: bytes) -> None:
        if self._muted or not pcm_bytes:
            return
        if self.wake_word_engine is not None and hasattr(self.wake_word_engine, 'feed_audio'):
            self.wake_word_engine.feed_audio(pcm_bytes)

    # ------------------------------------------------------------------
    # Barge-in guard
    # ------------------------------------------------------------------

    def barge_in(self):
        '''Interrupt current speech if user speaks during SPEAKING.'''
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

                if not text:
                    logger.info('Voice turn ignored (empty STT transcript after %.1fms)', stt_ms)
                    return

                dm = self._resolve_dialogue_manager()
                t_llm = time.perf_counter()
                response_text = dm.chat(text)
                llm_ms = round((time.perf_counter() - t_llm) * 1000.0, 1)

                self._route_actions(response_text)

                if self._muted:
                    logger.info('Voice turn skipping TTS (muted) | stt=%.1fms llm=%.1fms', stt_ms, llm_ms)
                    return

                raw_tts = getattr(dm, 'last_raw_tts_response', None) or response_text
                if raw_tts and self.tts_engine is not None:
                    self.set_state(VoiceState.SPEAKING)
                    try:
                        self.tts_engine.speak(raw_tts, blocking=False, turn_id=tid)
                    except TypeError:
                        self.tts_engine.speak(raw_tts, blocking=False)
                    tts_wait_ms = self._wait_for_speech()

                total_ms = round((time.perf_counter() - t_turn0) * 1000.0, 1)
                logger.info(
                    'Voice Turn Complete | stt=%.1fms llm=%.1fms tts_wait=%.1fms total=%.1fms | user_chars=%d reply_chars=%d',
                    stt_ms, llm_ms, tts_wait_ms, total_ms, len(text), len(response_text or ''),
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
    # Public convenience methods
    # ------------------------------------------------------------------

    def mute(self):
        '''Mute the pipeline.'''
        self._muted = True
        if self.wake_word_engine and hasattr(self.wake_word_engine, 'set_muted'):
            self.wake_word_engine.set_muted(True)
        if self.tts_engine and hasattr(self.tts_engine, 'stop_speaking'):
            self.tts_engine.stop_speaking()
        self.set_state(VoiceState.MUTED)

    def unmute(self):
        '''Unmute the pipeline.'''
        self._muted = False
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
            'speaking': self._speaking,
            'initialized': self._initialized,
            'tts_status': self.tts_engine.get_status() if self.tts_engine else None,
        }


# Self-test block

if __name__ == '__main__':
    print('=' * 60)
    print('C.E.L.E.S.T.E. VoiceOrchestrator - Self Test (Stage 3.4)')
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

    orch.barge_in()
    print('[TEST] Barge-in (not speaking): OK')

    status = orch.get_status()
    assert 'state' in status
    assert 'muted' in status
    assert isinstance(status['muted'], bool)
    print(f'[TEST] Status snapshot: {status["state"]}, muted={status["muted"]} - OK')

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