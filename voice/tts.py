"""Kokoro TTS with sentence-boundary buffering and binary frame streaming.

Implements §1 step 11 and §3 (Voice-specific handling) of
`AgenticProject.md`:

- **Sentence-level synthesis**: text streamed in via ``response:chunk``
  is buffered until a sentence boundary (``.``, ``!``, ``?``, or a
  flush on ``response:final``), then each sentence is synthesized and
  delivered as binary WebSocket frames preceded by a
  ``response:audio_start`` envelope.
- **Ordered audio queue**: sentences are synthesized and emitted in
  order so the client can queue frames sequentially.
- **Graceful degradation**: if TTS fails (model load, synthesis error),
  the ticket completes with text only — voice is an enhancement, not a
  completion dependency.  The ``TTSError`` is caught by the worker which
  proceeds to ``response:final`` without audio.
- **No ARQ for audio**: TTS frames go directly to the Gateway via the
  ``AudioFrameCallback``; they are never enqueued in ARQ.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("voice.tts")

# ---------------------------------------------------------------------------
# Configuration defaults.
# ---------------------------------------------------------------------------
TTS_SAMPLE_RATE = 24000  # Kokoro default output rate.
KOKORO_VOICE = "af_heart"
# Maximum characters per synthesis chunk (Kokoro handles ~500 well).
MAX_SYNTH_CHARS = 500


# ---------------------------------------------------------------------------
# Audio frame callback — how the session emits audio.
# ---------------------------------------------------------------------------
@runtime_checkable
class AudioFrameCallback(Protocol):
    """Async callable invoked for TTS audio events.

    The worker implements this to publish ``response:audio_start``,
    binary audio frames, and ``response:audio_end`` to the Redis Stream
    (and thence to WebSocket clients).
    """

    async def __call__(
        self,
        stream_id: str,
        event_type: str,  # "response:audio_start" | "audio:chunk" | "response:audio_end"
        audio: bytes | None = None,
        mime_type: str | None = None,
        sample_rate: int | None = None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Synthesizer protocol.
# ---------------------------------------------------------------------------
@runtime_checkable
class SynthesizerProtocol(Protocol):
    """Synthesizes a text sentence to PCM audio bytes."""

    async def synthesize(self, text: str) -> bytes: ...


class KokoroSynthesizer:
    """Wraps the ``kokoro`` package.

    Lazily imports ``kokoro`` so the voice package loads in environments
    without the model.  The pipeline is loaded on first ``synthesize``
    call (or eagerly via ``load()``).
    """

    def __init__(
        self,
        voice: str = KOKORO_VOICE,
        sample_rate: int = TTS_SAMPLE_RATE,
    ) -> None:
        self._voice = voice
        self._sample_rate = sample_rate
        self._pipeline: Any = None  # kokoro.KPipeline

    def load(self) -> None:
        """Eagerly load the Kokoro pipeline (raises if kokoro missing)."""
        if self._pipeline is not None:
            return
        from kokoro import KPipeline  # type: ignore[import-untyped]

        self._pipeline = KPipeline()

    async def synthesize(self, text: str) -> bytes:
        """Synthesize a text sentence to 16-bit PCM bytes.

        Returns mono 16-bit PCM at ``sample_rate``.  Raises ``TTSError``
        if synthesis fails.
        """
        import asyncio

        self.load()
        try:
            import numpy as np
        except ImportError as exc:
            raise TTSError("numpy required for Kokoro synthesis") from exc

        def _do_synth() -> bytes:
            # Kokoro returns (graphemes, audio_tensor) pairs.
            results = list(self._pipeline(text, voice=self._voice))
            if not results:
                return b""
            # Concatenate audio from all chunks.
            audio_arrays = []
            for _gs, audio in results:
                if hasattr(audio, "cpu"):
                    audio = audio.cpu().numpy()
                elif not isinstance(audio, np.ndarray):
                    audio = np.asarray(audio)
                audio_arrays.append(audio)
            if not audio_arrays:
                return b""
            combined = np.concatenate(audio_arrays)
            # Convert float32 [-1, 1] to int16 PCM.
            int16 = (combined * 32767).astype(np.int16)
            return int16.tobytes()

        try:
            return await asyncio.to_thread(_do_synth)
        except Exception as exc:
            raise TTSError(f"synthesis failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------
class TTSError(RuntimeError):
    """Raised when TTS fails — caller completes the ticket with text only."""


# ---------------------------------------------------------------------------
# Sentence boundary splitting.
# ---------------------------------------------------------------------------
# Match sentence-ending punctuation followed by whitespace or end-of-string.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+|\s*$")


def split_sentences(text: str) -> list[str]:
    """Split text into sentences at punctuation boundaries.

    Handles:
    - Periods, exclamation marks, question marks followed by whitespace.
    - Multiple sentences on one line.
    - Trailing text without terminal punctuation (returned as a final chunk).
    - Empty input → empty list.
    - Abbreviations like "Dr." are not specially handled (Kokoro handles
      them phonetically); this is a best-effort boundary for buffering.

    Examples:
    >>> split_sentences("Hello world.")
    ['Hello world.']
    >>> split_sentences("Hello. How are you? I am fine!")
    ['Hello.', 'How are you?', 'I am fine!']
    >>> split_sentences("")
    []
    >>> split_sentences("No punctuation here")
    ['No punctuation here']
    """
    if not text or not text.strip():
        return []
    # Split on sentence boundaries, keeping the punctuation with the sentence.
    parts = _SENTENCE_END_RE.split(text.strip())
    # Filter empty strings from the split.
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# TTSStreamBuffer — accumulates chunks and emits synthesized audio.
# ---------------------------------------------------------------------------
class TTSStreamBuffer:
    """Buffers ``response:chunk`` deltas and synthesizes per sentence.

    Lifecycle:
    1. ``feed_chunk(delta)`` — accumulate text from the worker's
       ``response:chunk`` events.
    2. When a sentence boundary is detected, the complete sentence is
       synthesized and emitted via the ``AudioFrameCallback``.
    3. ``flush()`` — called on ``response:final``; synthesizes any
       remaining buffered text.

    On synthesis failure, the buffer logs the error and continues — the
    ticket completes with text only (graceful degradation).
    """

    def __init__(
        self,
        stream_id: str,
        synthesizer: SynthesizerProtocol,
        callback: AudioFrameCallback,
        *,
        sample_rate: int = TTS_SAMPLE_RATE,
        mime_type: str = "audio/pcm",
        slo_tracker: Any | None = None,
    ) -> None:
        self.stream_id = stream_id
        self._synthesizer = synthesizer
        self._callback = callback
        self._sample_rate = sample_rate
        self._mime_type = mime_type
        self._slo = slo_tracker

        self._pending = ""  # text not yet flushed to a sentence
        self._started = False  # whether response:audio_start was sent
        self._flushed = False

    async def feed_chunk(self, delta: str) -> None:
        """Accumulate a text delta and synthesize complete sentences."""
        if self._flushed:
            return
        self._pending += delta

        # Split into sentences; the last element is incomplete (no
        # terminal punctuation) and stays in the buffer.
        sentences = split_sentences(self._pending)
        if len(sentences) <= 1:
            return  # No complete sentence yet.

        # All but the last are complete sentences.
        complete = sentences[:-1]
        self._pending = sentences[-1]

        for sentence in complete:
            await self._synthesize_and_emit(sentence)

    async def flush(self) -> None:
        """Synthesize any remaining buffered text and close the stream."""
        if self._flushed:
            return
        self._flushed = True
        if self._pending.strip():
            await self._synthesize_and_emit(self._pending)
            self._pending = ""
        if self._started:
            await self._callback(
                self.stream_id, "response:audio_end"
            )

    async def _synthesize_and_emit(self, sentence: str) -> None:
        """Synthesize one sentence and emit its audio frames."""
        text = sentence.strip()
        if not text:
            return

        import time as _time

        start = _time.monotonic()
        try:
            audio = await self._synthesizer.synthesize(text)
        except TTSError as exc:
            # Graceful degradation: log and skip this sentence.
            # The ticket completes with text only.
            logger.error("TTS synthesis failed for %s: %s", self.stream_id, exc)
            if self._slo is not None:
                self._slo.record_tts_latency(
                    (_time.monotonic() - start) * 1000, succeeded=False
                )
                self._slo.record_tts_failure(self.stream_id)
            return

        if self._slo is not None:
            self._slo.record_tts_latency(
                (_time.monotonic() - start) * 1000, succeeded=True
            )

        if not audio:
            return

        # Emit audio_start on first successful synthesis.
        if not self._started:
            await self._callback(
                self.stream_id,
                "response:audio_start",
                mime_type=self._mime_type,
                sample_rate=self._sample_rate,
            )
            self._started = True

        await self._callback(
            self.stream_id, "audio:chunk", audio=audio
        )


# ---------------------------------------------------------------------------
# TTSService — manages stream buffers by stream_id.
# ---------------------------------------------------------------------------
class TTSService:
    """Owns ``TTSStreamBuffer`` instances for active response streams.

    The worker calls ``start_stream`` before streaming ``response:chunk``,
    ``feed_chunk`` for each delta, and ``flush`` on ``response:final``.
    """

    def __init__(
        self,
        synthesizer: SynthesizerProtocol | None = None,
        slo_tracker: Any | None = None,
    ) -> None:
        self._synthesizer = synthesizer or KokoroSynthesizer()
        self._buffers: dict[str, TTSStreamBuffer] = {}
        self._slo = slo_tracker

    def start_stream(
        self,
        stream_id: str,
        callback: AudioFrameCallback,
    ) -> TTSStreamBuffer:
        """Create a new TTS buffer for ``stream_id``."""
        if stream_id in self._buffers:
            logger.warning("TTS buffer %s already exists — replacing", stream_id)
        buffer = TTSStreamBuffer(
            stream_id=stream_id,
            synthesizer=self._synthesizer,
            callback=callback,
            slo_tracker=self._slo,
        )
        self._buffers[stream_id] = buffer
        logger.info("started TTS buffer %s", stream_id)
        return buffer

    async def feed_chunk(self, stream_id: str, delta: str) -> None:
        """Feed a text delta to the buffer for ``stream_id``."""
        buffer = self._buffers.get(stream_id)
        if buffer is None:
            logger.debug("no TTS buffer for %s — dropping chunk", stream_id)
            return
        await buffer.feed_chunk(delta)

    async def flush(self, stream_id: str) -> None:
        """Flush and remove the buffer for ``stream_id``."""
        buffer = self._buffers.pop(stream_id, None)
        if buffer is None:
            return
        await buffer.flush()
        logger.info("flushed TTS buffer %s", stream_id)

    def get_buffer(self, stream_id: str) -> TTSStreamBuffer | None:
        return self._buffers.get(stream_id)

    async def health(self) -> bool:
        """Health check — verify the synthesizer can load."""
        try:
            if hasattr(self._synthesizer, "load"):
                self._synthesizer.load()  # type: ignore[attr-defined]
            return True
        except Exception as exc:
            logger.warning("TTS health check failed: %s", exc)
            return False
