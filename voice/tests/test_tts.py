"""Tests for the TTS (Text-to-Speech) service.

Covers:
- split_sentences: punctuation boundaries, multiple sentences, no punctuation, empty.
- TTSStreamBuffer accumulates chunks and synthesizes at sentence boundaries.
- TTSStreamBuffer flushes remaining text on flush().
- TTSStreamBuffer emits response:audio_start before first frame, audio_end on flush.
- TTSStreamBuffer graceful degradation: synthesis failure → skip, continue.
- TTSService manages buffers by stream_id (start/feed/flush).
- TTSService drops chunks for unknown stream_id.
- Ordered audio emission: sentences synthesized in order.
- Raw audio frames never enqueued in ARQ (callback only).
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from voice.tts import (
    TTSError,
    TTSService,
    TTSStreamBuffer,
    split_sentences,
)


# ---------------------------------------------------------------------------
# Helpers — mock synthesizer and callback.
# ---------------------------------------------------------------------------
class MockSynthesizer:
    """Configurable async synthesizer mock.

    Returns ``audio_bytes`` for each call.  Set ``fail=True`` to simulate
    TTS failure.  Records all calls in order.
    """

    def __init__(self, audio: bytes = b"\x00\x01" * 100, fail: bool = False):
        self._audio = audio
        self.fail = fail
        self.calls: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        if self.fail:
            raise TTSError("kokoro load failed")
        return self._audio


class MockFrameCallback:
    """Records all audio frame events emitted by the buffer."""

    def __init__(self):
        self.events: list[dict[str, Any]] = []

    async def __call__(
        self,
        stream_id: str,
        event_type: str,
        audio: bytes | None = None,
        mime_type: str | None = None,
        sample_rate: int | None = None,
    ) -> None:
        self.events.append(
            {
                "stream_id": stream_id,
                "event_type": event_type,
                "audio": audio,
                "mime_type": mime_type,
                "sample_rate": sample_rate,
            }
        )


# ---------------------------------------------------------------------------
# split_sentences tests.
# ---------------------------------------------------------------------------
class TestSplitSentences:
    def test_single_sentence_with_period(self):
        assert split_sentences("Hello world.") == ["Hello world."]

    def test_multiple_sentences(self):
        result = split_sentences("Hello. How are you? I am fine!")
        assert result == ["Hello.", "How are you?", "I am fine!"]

    def test_no_punctuation(self):
        assert split_sentences("No punctuation here") == ["No punctuation here"]

    def test_empty_string(self):
        assert split_sentences("") == []

    def test_whitespace_only(self):
        assert split_sentences("   ") == []

    def test_trailing_text_without_punctuation(self):
        result = split_sentences("Hello. Trailing text")
        assert result == ["Hello.", "Trailing text"]

    def test_multiple_spaces_between_sentences(self):
        result = split_sentences("First.  Second.")
        assert result == ["First.", "Second."]

    def test_exclamation_and_question(self):
        result = split_sentences("Wow! Really? Yes.")
        assert result == ["Wow!", "Really?", "Yes."]


# ---------------------------------------------------------------------------
# TTSStreamBuffer tests.
# ---------------------------------------------------------------------------
class TestTTSStreamBuffer:
    @pytest.mark.asyncio
    async def test_synthesizes_at_sentence_boundary(self):
        synth = MockSynthesizer()
        callback = MockFrameCallback()
        buffer = TTSStreamBuffer("s1", synth, callback)

        # Feed chunks that form two sentences.
        await buffer.feed_chunk("Hello ")
        await buffer.feed_chunk("world. How are")
        await buffer.feed_chunk(" you?")
        # The last sentence ("How are you?") is complete but still in the
        # buffer because split_sentences keeps the last element as pending.
        # Flush to synthesize it.
        await buffer.flush()

        # Two complete sentences synthesized.
        assert len(synth.calls) == 2
        assert synth.calls[0] == "Hello world."
        assert synth.calls[1] == "How are you?"

    @pytest.mark.asyncio
    async def test_flush_synthesizes_remaining_text(self):
        synth = MockSynthesizer()
        callback = MockFrameCallback()
        buffer = TTSStreamBuffer("s1", synth, callback)

        await buffer.feed_chunk("No punctuation")
        await buffer.flush()

        # Remaining text synthesized on flush.
        assert len(synth.calls) == 1
        assert synth.calls[0] == "No punctuation"

    @pytest.mark.asyncio
    async def test_emits_audio_start_before_first_frame(self):
        synth = MockSynthesizer()
        callback = MockFrameCallback()
        buffer = TTSStreamBuffer("s1", synth, callback)

        await buffer.feed_chunk("Hello.")
        await buffer.flush()

        # audio_start must come before audio:chunk.
        types = [e["event_type"] for e in callback.events]
        assert types[0] == "response:audio_start"
        assert "audio:chunk" in types
        assert types[-1] == "response:audio_end"

    @pytest.mark.asyncio
    async def test_emits_audio_end_on_flush(self):
        synth = MockSynthesizer()
        callback = MockFrameCallback()
        buffer = TTSStreamBuffer("s1", synth, callback)

        await buffer.feed_chunk("Hello.")
        await buffer.flush()

        ends = [e for e in callback.events if e["event_type"] == "response:audio_end"]
        assert len(ends) == 1

    @pytest.mark.asyncio
    async def test_no_audio_end_if_no_speech_synthesized(self):
        """If no text was ever fed, no audio_start/end should be emitted."""
        synth = MockSynthesizer()
        callback = MockFrameCallback()
        buffer = TTSStreamBuffer("s1", synth, callback)

        await buffer.flush()

        assert len(callback.events) == 0

    @pytest.mark.asyncio
    async def test_synthesis_failure_skips_sentence_and_continues(self):
        """Graceful degradation: TTS failure → skip, keep going."""
        synth = MockSynthesizer(fail=True)
        callback = MockFrameCallback()
        buffer = TTSStreamBuffer("s1", synth, callback)

        await buffer.feed_chunk("First. Second. Third.")
        await buffer.flush()

        # All three sentences attempted, none produced audio.
        assert len(synth.calls) == 3
        # No audio_start emitted because no audio was produced.
        starts = [e for e in callback.events if e["event_type"] == "response:audio_start"]
        assert len(starts) == 0

    @pytest.mark.asyncio
    async def test_ordered_audio_emission(self):
        """Sentences are synthesized and emitted in order."""
        call_order: list[str] = []

        class OrderedSynth:
            async def synthesize(self, text: str) -> bytes:
                call_order.append(text)
                return text.encode()

        callback = MockFrameCallback()
        buffer = TTSStreamBuffer("s1", OrderedSynth(), callback)

        await buffer.feed_chunk("First. Second. Third.")
        await buffer.flush()

        assert call_order == ["First.", "Second.", "Third."]

    @pytest.mark.asyncio
    async def test_feeds_after_flush_ignored(self):
        synth = MockSynthesizer()
        callback = MockFrameCallback()
        buffer = TTSStreamBuffer("s1", synth, callback)

        await buffer.feed_chunk("Hello.")
        await buffer.flush()
        await buffer.feed_chunk("More text.")  # should be ignored

        assert len(synth.calls) == 1

    @pytest.mark.asyncio
    async def test_empty_chunks_ignored(self):
        synth = MockSynthesizer()
        callback = MockFrameCallback()
        buffer = TTSStreamBuffer("s1", synth, callback)

        await buffer.feed_chunk("")
        await buffer.feed_chunk("   ")
        await buffer.flush()

        assert len(synth.calls) == 0


# ---------------------------------------------------------------------------
# TTSService tests.
# ---------------------------------------------------------------------------
class TestTTSService:
    @pytest.mark.asyncio
    async def test_start_feed_flush_lifecycle(self):
        synth = MockSynthesizer()
        service = TTSService(synthesizer=synth)
        callback = MockFrameCallback()

        service.start_stream("s1", callback)
        assert service.get_buffer("s1") is not None

        await service.feed_chunk("s1", "Hello.")
        await service.flush("s1")

        # Buffer removed after flush.
        assert service.get_buffer("s1") is None
        # Audio events emitted.
        assert len(callback.events) > 0

    @pytest.mark.asyncio
    async def test_feed_unknown_stream_drops_chunk(self):
        service = TTSService(synthesizer=MockSynthesizer())
        # Should not raise.
        await service.feed_chunk("unknown", "text")

    @pytest.mark.asyncio
    async def test_flush_unknown_stream_is_noop(self):
        service = TTSService(synthesizer=MockSynthesizer())
        await service.flush("unknown")  # should not raise

    @pytest.mark.asyncio
    async def test_start_replaces_existing_buffer(self):
        service = TTSService(synthesizer=MockSynthesizer())
        cb1 = MockFrameCallback()
        cb2 = MockFrameCallback()

        service.start_stream("s1", cb1)
        service.start_stream("s1", cb2)

        assert service.get_buffer("s1") is not None

    @pytest.mark.asyncio
    async def test_health_returns_true_when_synthesizer_loads(self):
        class HealthySynth:
            def load(self):
                pass

            async def synthesize(self, text: str) -> bytes:
                return b""

        service = TTSService(synthesizer=HealthySynth())
        assert await service.health() is True

    @pytest.mark.asyncio
    async def test_health_returns_false_on_load_failure(self):
        class BrokenSynth:
            def load(self):
                raise RuntimeError("kokoro missing")

            async def synthesize(self, text: str) -> bytes:
                return b""

        service = TTSService(synthesizer=BrokenSynth())
        assert await service.health() is False


# ---------------------------------------------------------------------------
# Integration: chunk → sentence → synth → callback (no ARQ).
# ---------------------------------------------------------------------------
class TestTTSIntegration:
    @pytest.mark.asyncio
    async def test_full_flow_no_arq_enqueue(self):
        """Verify audio goes through the callback, never ARQ."""
        synth = MockSynthesizer(audio=b"\x01\x02\x03\x04")
        callback = MockFrameCallback()
        service = TTSService(synthesizer=synth)

        service.start_stream("s1", callback)
        await service.feed_chunk("s1", "Hello world. How are you?")
        await service.flush("s1")

        # Events emitted through callback only.
        types = [e["event_type"] for e in callback.events]
        assert "response:audio_start" in types
        assert "audio:chunk" in types
        assert "response:audio_end" in types
        # Two sentences → two audio chunks.
        chunks = [e for e in callback.events if e["event_type"] == "audio:chunk"]
        assert len(chunks) == 2
