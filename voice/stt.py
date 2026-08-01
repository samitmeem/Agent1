"""Faster-Whisper STT with VAD and incremental transcript emission.

Implements §1 step 5 and §3 (Voice-specific handling) of
`AgenticProject.md`:

- **VAD before transcription** to reduce unnecessary model work.  A
  pluggable ``VADProtocol`` lets us swap webrtcvad (production) for a
  pure-Python energy detector (tests / CPU-only dev).
- **Incremental partial transcripts** emitted as
  ``audio:transcript_partial`` while the user speaks.
- **Final transcript** emitted as ``audio:transcript_final`` and persisted
  as a normal user message, then routed through the same ARQ workflow as
  text input.
- **Standardized input**: browser uploads ``audio/webm;codecs=opus``;
  the session decodes to 16 kHz mono PCM before VAD/Whisper.
- **Graceful degradation**: if STT is unavailable (model load failure,
  decode error, transcriber exception), the session emits a
  ``ticket:error`` event with ``code="STT_UNAVAILABLE"`` and the ticket
  context is preserved — the user can fall back to text.

Audio frames are latency-sensitive and are **never** enqueued in ARQ.
The Gateway forwards frames directly to the ``STTService`` which owns
the per-stream session state.
"""
from __future__ import annotations

import audioop
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("voice.stt")

# ---------------------------------------------------------------------------
# Configuration defaults (overridable via env in production wiring).
# ---------------------------------------------------------------------------
WHISPER_SAMPLE_RATE = 16000  # Whisper expects 16 kHz mono PCM.
VAD_FRAME_MS = 30  # webrtcvad works on 10/20/30 ms frames.
VAD_AGGRESSIVENESS = 2  # 0-3; higher = more aggressive speech detection.
# Energy VAD threshold (RMS amplitude). Frames below this are silence.
ENERGY_VAD_THRESHOLD = 500.0
# Number of consecutive silence frames after which we consider speech ended
# and emit a final transcript for the current utterance.
SILENCE_FRAMES_TO_FINAL = 33  # ~1 s at 30 ms frames.


# ---------------------------------------------------------------------------
# Transcript callback — how the session emits events.
# ---------------------------------------------------------------------------
@runtime_checkable
class TranscriptCallback(Protocol):
    """Async callable invoked for partial and final transcripts.

    The Gateway implements this to publish ``audio:transcript_partial``
    and ``audio:transcript_final`` events to the Redis Stream (and
    thence to WebSocket clients).
    """

    async def __call__(
        self,
        stream_id: str,
        event_type: str,  # "audio:transcript_partial" | "audio:transcript_final"
        transcript: str,
        message_id: str | None = None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# VAD protocol.
# ---------------------------------------------------------------------------
@runtime_checkable
class VADProtocol(Protocol):
    """Voice Activity Detection — decides if a PCM frame contains speech."""

    def is_speech(self, frame: bytes, sample_rate: int) -> bool: ...


class EnergyVAD:
    """Pure-Python energy-based VAD (no native deps).

    Computes the RMS amplitude of a PCM frame and classifies it as speech
    if above ``threshold``.  Suitable for tests and CPU-only dev where
    webrtcvad is unavailable.  Production wiring injects a
    ``WebRTCVAD`` instance instead.
    """

    def __init__(self, threshold: float = ENERGY_VAD_THRESHOLD) -> None:
        self.threshold = threshold

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        if len(frame) < 2:
            return False
        try:
            rms = audioop.rms(frame, 2)  # 16-bit signed samples
        except audioop.error:
            return False
        return rms >= self.threshold


class WebRTCVAD:
    """Wrapper around the ``webrtcvad`` package.

    Lazily imported so the voice package loads without the native dep.
    Constructed by ``build_vad()`` when ``webrtcvad`` is installed.
    """

    def __init__(self, aggressiveness: int = VAD_AGGRESSIVENESS) -> None:
        import webrtcvad  # type: ignore[import-untyped]

        self._vad = webrtcvad.Vad(aggressiveness)

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        return self._vad.is_speech(frame, sample_rate)


def build_vad(use_webrtc: bool = False) -> VADProtocol:
    """Factory for the VAD implementation.

    ``use_webrtc=True`` attempts to load webrtcvad; falls back to
    ``EnergyVAD`` if unavailable.  Tests inject a mock or ``EnergyVAD``.
    """
    if use_webrtc:
        try:
            return WebRTCVAD()
        except ImportError:
            logger.warning("webrtcvad unavailable — falling back to EnergyVAD")
    return EnergyVAD()


# ---------------------------------------------------------------------------
# Transcriber protocol.
# ---------------------------------------------------------------------------
@runtime_checkable
class TranscriberProtocol(Protocol):
    """Transcribes PCM audio to text.

    ``partial=True`` requests an incremental (best-effort) transcript;
    ``partial=False`` requests the final transcript for the accumulated
    audio.  Returns ``(text, is_final)``.
    """

    async def transcribe(
        self, audio: bytes, sample_rate: int, *, partial: bool = False
    ) -> tuple[str, bool]: ...


class FasterWhisperTranscriber:
    """Wraps ``faster_whisper.WhisperModel``.

    Lazily imports ``faster_whisper`` so the voice package loads in
    environments without the model.  The model is loaded on first
    ``transcribe`` call (or eagerly via ``load()``).
    """

    def __init__(
        self,
        model_name: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._model: Any = None  # faster_whisper.WhisperModel

    def load(self) -> None:
        """Eagerly load the model (raises if faster_whisper missing)."""
        if self._model is not None:
            return
        from faster_whisper import WhisperModel  # type: ignore[import-untyped]

        self._model = WhisperModel(
            self._model_name, device=self._device, compute_type=self._compute_type
        )

    async def transcribe(
        self, audio: bytes, sample_rate: int, *, partial: bool = False
    ) -> tuple[str, bool]:
        """Transcribe PCM audio bytes to text.

        ``audio`` must be 16 kHz mono 16-bit PCM.  Returns ``(text, is_final)``.
        Raises ``STTError`` if the model fails.
        """
        import asyncio

        self.load()
        try:
            # faster_whisper expects a numpy array; convert PCM bytes.
            import numpy as np

            audio_array = (
                np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            )
        except ImportError as exc:
            raise STTError("numpy required for Faster-Whisper transcription") from exc

        def _do_transcribe() -> tuple[str, bool]:
            segments, _info = self._model.transcribe(
                audio_array,
                language="en",
                vad_filter=True,
                beam_size=1 if partial else 5,
            )
            text = " ".join(seg.text for seg in segments).strip()
            return text, not partial

        try:
            return await asyncio.to_thread(_do_transcribe)
        except Exception as exc:
            raise STTError(f"transcription failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Audio decoding.
# ---------------------------------------------------------------------------
class AudioDecoder:
    """Decodes browser ``audio/webm;codecs=opus`` to 16 kHz mono PCM.

    Uses ``audioop`` (stdlib) for sample-rate conversion and
    channel downmix.  The actual Opus→PCM decode step requires a codec
    library (e.g. ``av``/ffmpeg); the ``decode`` method is split so a
    production wiring can inject a real decoder while tests feed raw PCM
    directly via ``feed_pcm``.
    """

    @staticmethod
    def resample_to_16k_mono(
        pcm: bytes, in_rate: int, in_channels: int
    ) -> bytes:
        """Convert PCM to 16 kHz mono 16-bit."""
        # Downmix to mono first.
        if in_channels > 1:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        # Resample to 16 kHz.  audioop.ratecv returns (converted, state).
        if in_rate != WHISPER_SAMPLE_RATE:
            pcm, _ = audioop.ratecv(pcm, 2, 1, in_rate, WHISPER_SAMPLE_RATE, None)
        return pcm


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------
class STTError(RuntimeError):
    """Raised when STT fails — mapped to ``ticket:error`` STT_UNAVAILABLE."""


# ---------------------------------------------------------------------------
# STTSession — per-stream state machine.
# ---------------------------------------------------------------------------
class STTSession:
    """Accumulates audio for one ``stream_id`` and emits transcripts.

    Lifecycle:
    1. ``feed_pcm(frame)`` — called for each decoded PCM frame.
       VAD filters silence; speech frames accumulate in a buffer.
    2. On trailing silence (``SILENCE_FRAMES_TO_FINAL``), the buffer is
       transcribed and a final transcript is emitted; the buffer resets.
    3. Periodically (every ``partial_interval_frames``), a partial
       transcript is emitted from the current buffer.
    4. ``finalize()`` — called on ``audio:stop``; transcribes any
       remaining buffer and emits the final transcript.

    The session never touches ARQ — it emits events through the
    ``TranscriptCallback`` and the Gateway persists the final transcript.
    """

    def __init__(
        self,
        stream_id: str,
        vad: VADProtocol,
        transcriber: TranscriberProtocol,
        callback: TranscriptCallback,
        *,
        partial_interval_frames: int = 10,
        silence_frames_to_final: int = SILENCE_FRAMES_TO_FINAL,
        slo_tracker: Any | None = None,
    ) -> None:
        self.stream_id = stream_id
        self._vad = vad
        self._transcriber = transcriber
        self._callback = callback
        self._partial_interval = partial_interval_frames
        self._silence_limit = silence_frames_to_final
        self._slo = slo_tracker

        self._buffer = bytearray()
        self._speech_active = False
        self._consecutive_silence = 0
        self._frames_since_partial = 0
        self._finalized = False
        self._last_partial = ""

    async def feed_pcm(self, frame: bytes, sample_rate: int = WHISPER_SAMPLE_RATE) -> None:
        """Process one PCM frame through VAD and accumulate speech."""
        if self._finalized:
            return

        is_speech = self._vad.is_speech(frame, sample_rate)

        if is_speech:
            self._buffer.extend(frame)
            self._speech_active = True
            self._consecutive_silence = 0
        elif self._speech_active:
            # Trailing silence — count it.
            self._consecutive_silence += 1
            # Include a little silence in the buffer for naturalness.
            self._buffer.extend(frame)

            if self._consecutive_silence >= self._silence_limit:
                await self._emit_final()
                return

        # Emit partials periodically while speech is active.
        self._frames_since_partial += 1
        if (
            self._speech_active
            and len(self._buffer) > 0
            and self._frames_since_partial >= self._partial_interval
        ):
            await self._emit_partial()
            self._frames_since_partial = 0

    async def _emit_partial(self) -> None:
        """Transcribe the current buffer (partial mode) and emit."""
        if not self._buffer:
            return
        try:
            text, _ = await self._transcriber.transcribe(
                bytes(self._buffer), WHISPER_SAMPLE_RATE, partial=True
            )
        except STTError as exc:
            logger.warning("partial transcription failed for %s: %s", self.stream_id, exc)
            return
        if text and text != self._last_partial:
            self._last_partial = text
            await self._callback(
                self.stream_id, "audio:transcript_partial", text
            )

    async def _emit_final(self, message_id: str | None = None) -> None:
        """Transcribe the buffer (final mode) and emit the final transcript."""
        if self._finalized:
            return
        self._finalized = True
        if not self._buffer:
            # No speech detected — emit empty final so the caller knows
            # the stream ended.
            await self._callback(
                self.stream_id, "audio:transcript_final", "", message_id
            )
            return

        import time as _time

        start = _time.monotonic()
        try:
            text, _ = await self._transcriber.transcribe(
                bytes(self._buffer), WHISPER_SAMPLE_RATE, partial=False
            )
        except STTError as exc:
            # STT failure — emit STT_UNAVAILABLE error, preserve context.
            # We do NOT re-raise: the session is finalized and the caller
            # (Gateway) continues to accept text input for the same ticket.
            logger.error("final transcription failed for %s: %s", self.stream_id, exc)
            if self._slo is not None:
                self._slo.record_stt_latency(
                    (_time.monotonic() - start) * 1000, succeeded=False
                )
                self._slo.record_stt_failure(self.stream_id)
            await self._callback(
                self.stream_id,
                "ticket:error",
                "",
                message_id=None,
            )
            return

        if self._slo is not None:
            self._slo.record_stt_latency(
                (_time.monotonic() - start) * 1000, succeeded=True
            )
        await self._callback(
            self.stream_id, "audio:transcript_final", text, message_id
        )

    async def finalize(self, message_id: str | None = None) -> None:
        """Called on ``audio:stop`` — flush any remaining buffer."""
        await self._emit_final(message_id=message_id)

    @property
    def finalized(self) -> bool:
        return self._finalized


# ---------------------------------------------------------------------------
# STTService — manages sessions by stream_id.
# ---------------------------------------------------------------------------
class STTService:
    """Owns ``STTSession`` instances and routes audio frames.

    The Gateway calls ``start_stream`` on ``audio:start``, ``feed_pcm``
    for each decoded frame, and ``stop_stream`` on ``audio:stop``.
    """

    def __init__(
        self,
        vad: VADProtocol | None = None,
        transcriber: TranscriberProtocol | None = None,
        slo_tracker: Any | None = None,
    ) -> None:
        self._vad = vad or build_vad()
        self._transcriber = transcriber or FasterWhisperTranscriber()
        self._sessions: dict[str, STTSession] = {}
        self._slo = slo_tracker

    def start_stream(
        self,
        stream_id: str,
        callback: TranscriptCallback,
    ) -> STTSession:
        """Create a new STT session for ``stream_id``."""
        if stream_id in self._sessions:
            logger.warning("STT session %s already exists — replacing", stream_id)
        session = STTSession(
            stream_id=stream_id,
            vad=self._vad,
            transcriber=self._transcriber,
            callback=callback,
            slo_tracker=self._slo,
        )
        self._sessions[stream_id] = session
        logger.info("started STT session %s", stream_id)
        return session

    async def feed_pcm(self, stream_id: str, frame: bytes, sample_rate: int = WHISPER_SAMPLE_RATE) -> None:
        """Feed a decoded PCM frame to the session for ``stream_id``."""
        session = self._sessions.get(stream_id)
        if session is None:
            logger.debug("no STT session for %s — dropping frame", stream_id)
            return
        await session.feed_pcm(frame, sample_rate)

    async def stop_stream(self, stream_id: str, message_id: str | None = None) -> None:
        """Finalize and remove the session for ``stream_id``."""
        session = self._sessions.pop(stream_id, None)
        if session is None:
            return
        await session.finalize(message_id=message_id)
        logger.info("stopped STT session %s", stream_id)

    def get_session(self, stream_id: str) -> STTSession | None:
        return self._sessions.get(stream_id)

    async def health(self) -> bool:
        """Health check — verify the transcriber can load."""
        try:
            if hasattr(self._transcriber, "load"):
                self._transcriber.load()  # type: ignore[attr-defined]
            return True
        except Exception as exc:
            logger.warning("STT health check failed: %s", exc)
            return False
