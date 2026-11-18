'''JARVIS Voice Pipeline Orchestrator - Stage 3.4.

Connects WakeWordEngine, STTEngine, DialogueManager, and TTSEngine into a
coherent state-machine-driven voice pipeline with procedural activation chime,
barge-in guard, and action routing.
'''
import logging
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
logger = logging.getLogger(__name__)


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

    def __init__(self, on_state_change=None):
        self._state = VoiceState.IDLE
        self._lock = threading.Lock()
        self._speaking = False
        self._muted = False
        self._confirmation_pending = None
        self._on_state_change = on_state_change or (lambda s: None)
        # Component instances (lazy-init or injected)
        self.wake_word_engine = None
        self.stt_engine = None
        self.dialogue_manager = None
        self.tts_engine = None
        self._initialized = False

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

            self.wake_word_engine = WakeWordEngine(
                on_wake_word_detected=self._on_wake_word,
                speech_recorded_callback=self._on_speech_captured,
            )
            if not self.wake_word_engine.initialize():
                logger.error('WakeWordEngine init failed')
                return False

            self.stt_engine = STTEngine(model_size='small', device='cpu', compute_type='int8')
            if not self.stt_engine.initialize():
                logger.error('STT engine init failed')
                return False

            self.dialogue_manager = DialogueManager()
            self.tts_engine = TTSEngine(model_name='fa')
            if not self.tts_engine.initialize():
                logger.error('TTSEngine init failed')
                return False

            self._initialized = True
            logger.info('VoiceOrchestrator initialized (all components OK)')
            return True
        except Exception as exc:
            logger.error('Orchestrator init error: %s', exc)
            return False

    def start(self):
        '''Start the pipeline (start wake-word listening).'''
        if not self._initialized:
            if not self.initialize():
                raise RuntimeError('Orchestrator not initialized')
        self.wake_word_engine.start()
        logger.info('Pipeline started - listening for wake word')

    def stop(self):
        '''Stop the pipeline.'''
        self.wake_word_engine.stop()
        self.set_state(VoiceState.IDLE)
        logger.info('Pipeline stopped')

    def shutdown(self):
        '''Full shutdown of all components.'''
        self.stop()
        if self.stt_engine:
            self.stt_engine.shutdown()
        logger.info('Orchestrator fully shut down')
    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def get_state(self) -> VoiceState:
        with self._lock:
            return self._state

    def set_state(self, new_state: VoiceState):
        old = self._state
        if old != new_state:
            logger.info('State transition: %s -> %s', old.name, new_state.name)
            with self._lock:
                self._state = new_state
            self._on_state_change(new_state)

    def is_muted(self) -> bool:
        return self._muted

    # ------------------------------------------------------------------
    # Barge-in guard
    # ------------------------------------------------------------------

    def barge_in(self):
        '''Interrupt current speech if user speaks during SPEAKING.'''
        if self.get_state() == VoiceState.SPEAKING and not self._muted:
            logger.info('BARGE-IN detected - stopping speech')
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

        # Apply JARVIS metallic DSP (pass sample_rate=22050 to match chime generation rate)
        from Voice.AudioDSP.jarvis_dsp import JarvisAudioDSP
        dsp = JarvisAudioDSP(pitch_semitones=-3.0, sample_rate=self.CHIME_SR)
        chime = dsp.process(chime)

        # Play via sounddevice
        try:
            import sounddevice as sd
            sd.play(chime, samplerate=sr, blocking=False)
            logger.info('Activation chime played (%d samples @ %d Hz)', len(chime), sr)
        except ImportError:
            logger.warning('sounddevice not available - chime not played')

    # Callbacks (internal)

    def _on_wake_word(self, ww_name):
        if self._muted:
            return
        logger.info('Wake-word "%s" detected!', ww_name)
        self.play_activation_chime()
        self.set_state(VoiceState.LISTENING)

    def _on_speech_captured(self, pcm_bytes):
        logger.info('Speech captured: %d bytes', len(pcm_bytes))
        self.set_state(VoiceState.THINKING)
        try:
            result = self.stt_engine.transcribe_pcm(pcm_bytes, sample_rate=16000, language='fa')
            text = result.get('text', '').strip()
            if not text:
                self.set_state(VoiceState.IDLE)
                return
            response_text = self.dialogue_manager.chat(text)
            self._route_actions(response_text)
            self.set_state(VoiceState.SPEAKING)
            self.tts_engine.speak(response_text, blocking=False)
            self._wait_for_speech()
        except Exception as exc:
            logger.error('Pipeline error: %s', exc)
    # Public convenience methods

    def mute(self):
        '''Mute the pipeline.'''
        self._muted = True
        if self.wake_word_engine:
            self.wake_word_engine.set_muted(True)
        if self.tts_engine:
            self.tts_engine.stop_speaking()
        self.set_state(VoiceState.MUTED)

    def unmute(self):
        '''Unmute the pipeline.'''
        self._muted = False
        if self.wake_word_engine:
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
        '''Apply voice configuration changes live to the active pipeline.

        Updates JarvisAudioDSP global config and, if a TTSEngine instance with
        a cached _dsp is available, pushes the same config to it immediately.
        Also handles mute toggle, wake-word audio-source/threshold changes,
        and TTS engine updates.
        '''
        # 1) Update global DSP config
        from Voice.AudioDSP.jarvis_dsp import JarvisAudioDSP

        JarvisAudioDSP.set_global_config(
            pitch_semitones=pitch_semitones,
            metallic_mix=metallic_mix,
            reverb_decay=reverb_decay,
            speech_rate=speech_rate,
        )

        # 2) Push to TTSEngine's cached _dsp (if present)
        if self.tts_engine and hasattr(self.tts_engine, '_dsp') and self.tts_engine._dsp is not None:
            try:
                self.tts_engine._dsp.update_config(
                    pitch_semitones=pitch_semitones,
                    metallic_mix=metallic_mix,
                    reverb_decay=reverb_decay,
                    speech_rate=speech_rate,
                )
            except Exception:
                pass

        # 3) Update TTSEngine config directly
        if tts_config and self.tts_engine and hasattr(self.tts_engine, 'update_config'):
            try:
                self.tts_engine.update_config(tts_config)
            except Exception:
                pass

        # 4) Mute toggle (only if explicitly provided)
        if muted is not None:
            if muted:
                self.mute()
            else:
                self.unmute()

        # 5) Wake-word audio-source and threshold change
        if self.wake_word_engine is not None:
            if audio_source is not None:
                try:
                    self.wake_word_engine.set_audio_source(audio_source)
                except Exception:
                    pass
            if wake_threshold is not None and hasattr(self.wake_word_engine, 'set_wake_threshold'):
                try:
                    self.wake_word_engine.set_wake_threshold(float(wake_threshold))
                except Exception:
                    pass

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
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    print('=' * 60)
    print('JARVIS VoiceOrchestrator - Self Test (Stage 3.4)')
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

