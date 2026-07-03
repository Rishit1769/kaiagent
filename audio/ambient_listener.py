"""
Always-on ambient audio listener.
Handles:
  - Continuous mic stream (single sounddevice input)
  - Energy-based VAD to detect speech segments
  - Wake-word detection via faster-whisper tiny model (triggers on "kai agent" / "hey kai agent")
  - Push-to-talk buffering when hotkey is held
  - Streams RMS level to UI (cursor waveform + panel)
"""

import threading
import time
from enum import Enum, auto
import logging
from typing import Any, Callable, Optional

import numpy as np

from audio.capture import (
    SAMPLE_RATE,
    SOUNDDEVICE_IMPORT_ERROR,
    audio_backend_available,
    pcm16_to_wav,
    sd,
)


logger = logging.getLogger(__name__)


class Mode(Enum):
    STANDBY       = auto()   # wake-word scanning
    RECORDING     = auto()   # actively buffering user utterance


# â”€â”€ Tuning knobs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
BLOCK_MS           = 30              # mic callback granularity
FRAMES_PER_BLOCK   = int(SAMPLE_RATE * BLOCK_MS / 1000)
ENERGY_THRESHOLD   = 0.006           # lower = catches quieter speech
MIN_SPEECH_BLOCKS  = 3               # ~90ms of speech to start a segment
SILENCE_BLOCKS_END = 20              # ~600ms of silence ends a segment
MAX_SEGMENT_BLOCKS = 120             # ~3.6s max wake-word segment
PRE_ROLL_BLOCKS    = 18              # ~540ms of pre-roll for the wake word

# Wake phrases â€” whisper tiny often mis-transcribes "Kai Agent" on short clips,
# so we cover a few likely variants.
WAKE_WORDS = (
    "kai agent",
    "kaiagent",
    "kay agent",
    "kye agent",
    "kai ajent",
    "hey kai agent",
    "hi kai agent",
    "ok kai agent",
    "yo kai agent",
)


class AmbientListener:
    """
    Single sounddevice input stream with three outputs:
      1. level callback (always): drives cursor/panel waveform
      2. wake-word callback (standby): transcribes VAD segments with tiny whisper
      3. recording buffer (recording): full PCM buffer returned on stop_recording()
    """

    def __init__(
        self,
        on_level: Callable[[float], None],
        on_wake: Callable[[], None],
    ):
        self._on_level = on_level
        self._on_wake = on_wake

        self._mode: Mode = Mode.STANDBY
        self._stream: Optional[Any] = None
        self._running = False
        self._audio_available = audio_backend_available()

        # Rolling pre-roll ring buffer (small)
        self._preroll: list[np.ndarray] = []
        # Current speech segment buffer (for wake-word transcription)
        self._seg_buffer: list[np.ndarray] = []
        self._seg_speech_blocks = 0
        self._seg_silence_blocks = 0
        self._in_segment = False

        # Recording buffer (hotkey push-to-talk OR post-wake capture)
        self._rec_buffer: list[bytes] = []
        self._rec_lock = threading.Lock()

        # Lazy tiny whisper for wake word
        self._wake_model = None
        self._wake_lock = threading.Lock()
        self._wake_inflight = False

        # Enable/disable toggle
        self._wake_word_enabled = True

    # â”€â”€ Public API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def start(self):
        if self._running:
            return
        if not self._audio_available:
            logger.warning(
                "Ambient listener disabled because sounddevice is unavailable."
            )
            self._wake_word_enabled = False
            return
        self._running = True
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAMES_PER_BLOCK,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self):
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def start_recording(self) -> None:
        """Switch to RECORDING mode; all audio buffered for STT."""
        with self._rec_lock:
            self._rec_buffer = []
        self._mode = Mode.RECORDING

    def stop_recording(self) -> bytes:
        """Return buffered PCM16 bytes and resume standby."""
        with self._rec_lock:
            pcm = b"".join(self._rec_buffer)
            self._rec_buffer = []
        self._mode = Mode.STANDBY
        self._reset_segment()
        return pcm

    def snapshot_recording(self) -> bytes:
        """Return a thread-safe snapshot of the current recording buffer."""
        with self._rec_lock:
            return b"".join(self._rec_buffer)

    def set_wake_word_enabled(self, enabled: bool):
        self._wake_word_enabled = enabled

    @property
    def wake_word_enabled(self) -> bool:
        return self._wake_word_enabled

    @property
    def audio_available(self) -> bool:
        return self._audio_available

    @property
    def audio_error(self) -> Optional[Exception]:
        return SOUNDDEVICE_IMPORT_ERROR

    # â”€â”€ Audio callback â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _callback(self, indata: np.ndarray, frames: int, time_info, status):
        if not self._running:
            return

        pcm_int16 = indata[:, 0] if indata.ndim == 2 else indata
        pcm_float = pcm_int16.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(pcm_float ** 2)))
        self._on_level(rms)

        if self._mode == Mode.RECORDING:
            print("Audio chunk captured")
            with self._rec_lock:
                self._rec_buffer.append(pcm_int16.tobytes())
            return

        # Standby: VAD-based segment capture for wake-word
        if not self._wake_word_enabled:
            return

        # Maintain tiny pre-roll
        self._preroll.append(pcm_int16.copy())
        if len(self._preroll) > PRE_ROLL_BLOCKS:
            self._preroll.pop(0)

        is_speech = rms > ENERGY_THRESHOLD

        if not self._in_segment:
            if is_speech:
                self._seg_speech_blocks += 1
                self._seg_buffer.append(pcm_int16.copy())
                if self._seg_speech_blocks >= MIN_SPEECH_BLOCKS:
                    self._in_segment = True
                    # Prepend pre-roll so we catch the start of the word
                    self._seg_buffer = list(self._preroll) + self._seg_buffer
            else:
                self._seg_speech_blocks = max(0, self._seg_speech_blocks - 1)
                if not self._seg_speech_blocks:
                    self._seg_buffer = []
            return

        # In-segment
        self._seg_buffer.append(pcm_int16.copy())
        if is_speech:
            self._seg_silence_blocks = 0
        else:
            self._seg_silence_blocks += 1

        end = (
            self._seg_silence_blocks >= SILENCE_BLOCKS_END
            or len(self._seg_buffer) >= MAX_SEGMENT_BLOCKS
        )
        if end:
            seg = np.concatenate(self._seg_buffer).astype(np.int16).tobytes()
            self._reset_segment()
            self._dispatch_wake_check(seg)

    def _reset_segment(self):
        self._seg_buffer = []
        self._seg_speech_blocks = 0
        self._seg_silence_blocks = 0
        self._in_segment = False

    # â”€â”€ Wake-word transcription (off the audio thread) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _dispatch_wake_check(self, pcm: bytes):
        if self._wake_inflight:
            return
        self._wake_inflight = True
        t = threading.Thread(target=self._check_wake, args=(pcm,), daemon=True)
        t.start()

    def _check_wake(self, pcm: bytes):
        try:
            text = self._transcribe_tiny(pcm).lower().strip()
            if not text:
                return
            if any(w in text for w in WAKE_WORDS):
                self._on_wake()
        except Exception:
            pass
        finally:
            self._wake_inflight = False

    def _transcribe_tiny(self, pcm: bytes) -> str:
        """Pad PCM with silence (whisper accuracy degrades on ultra-short clips)."""
        import tempfile, os
        model = self._get_model()
        pad = bytes(int(SAMPLE_RATE * 0.4) * 2)    # 400ms silence each side
        padded = pad + pcm + pad
        wav = pcm16_to_wav(padded)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
            path = f.name
        try:
            segments, _ = model.transcribe(
                path,
                beam_size=5,
                language="en",
                condition_on_previous_text=False,
                no_speech_threshold=0.45,
                temperature=0.0,
                initial_prompt="Kai Agent is a helpful AI assistant.",
            )
            return " ".join(s.text for s in segments)
        finally:
            os.unlink(path)

    def _get_model(self):
        with self._wake_lock:
            if self._wake_model is None:
                from faster_whisper import WhisperModel
                self._wake_model = WhisperModel(
                    "tiny.en", device="cpu", compute_type="int8"
                )
            return self._wake_model

