"""Tests for the STT (Speech-to-Text) service.

Covers:
- EnergyVAD classifies speech vs silence by RMS amplitude.
- build_vad returns EnergyVAD when webrtcvad unavailable.
- STTSession accumulates speech frames and emits partials.
- STTSession emits final transcript on trailing silence.
- STTSession emits final transcript on finalize() (audio:stop).
- STTSession emits empty final when no speech detected.
- STTSession emits STT_UNAVAILABLE (ticket:error) on transcriber failure.
- STTService manages sessions by stream_id (start/feed/stop).
- STTService drops frames for unknown stream_id.
- AudioDecoder resamples to 16 kHz mono.
- Graceful degradation: STT failure preserves ticket context.
- Raw audio frames are never enqueued in ARQ (callback only).
"""
from __future__ import annotations

import asyncio
import struct
from typing import Any
from uuid import uuid4

import pytest
from voice.stt import (
    AudioDecoder,
    EnergyVAD,
    STTError,
    STTService,
    STTSession,
    build_vad,
)


# ---------------------------------------------------------------------------
# Helpers — mock transcriber and callback.
# ---------------------------------------------------------------------------
class MockTranscriber:
    """Configurable async transcriber mock.

    ``partial_text`` is returned for partial calls; ``final_text`` for
    final calls.  Set ``fail=True`` to simulate STT failure.
    """

    def __init__(
        self,
        partial_text: str = "hello",
        final_text: str = "hello world",
        fail: bool = False,
    ):
        self.partial_text = partial_text
        self.final_text = final_text
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def transcribe(
        self, audio: bytes, sample_rate: int, *, partial: bool = False
    ) -> tuple[str, bool]:
        self.calls.append({"audio_len": len(audio), "partial": partial})
        if self.fail:
            raise STTError("model load failed")
        if partial:
            return self.partial_text, False
        return self.final_text, True


class MockCallback:
    """Records all transcript events emitted by the session."""

    def __init__(self):
        self.events: list[dict[str, Any]] = []

    async def __call__(
        self,
        stream_id: str,
        event_type: str,
        transcript: str,
        message_id: str | None = None,
    ) -> None:
        self.events.append(
            {
                "stream_id": stream_id,
                "event_type": event_type,
                "transcript": transcript,
                "message_id": message_id,
            }
        )


def _speech_frame(amplitude: int = 10000) -> bytes:
    """Build a 30 ms 16 kHz mono frame with the given amplitude."""
    # 30 ms at 16 kHz = 480 samples, 16-bit = 960 bytes.
    return struct.pack(f"<{480}h", *([amplitude] * 480))


def _silence_frame() -> bytes:
    """Build a 30 ms 16 kHz mono silence frame."""
    return b"\x00" * 960


# ---------------------------------------------------------------------------
# EnergyVAD tests.
# ---------------------------------------------------------------------------
class TestEnergyVAD:
    def test_speech_detected_above_threshold(self):
        vad = EnergyVAD(threshold=500)
        assert vad.is_speech(_speech_frame(10000), 16000) is True

    def test_silence_below_threshold(self):
        vad = EnergyVAD(threshold=500)
        assert vad.is_speech(_silence_frame(), 16000) is False

    def test_empty_frame_is_silence(self):
        vad = EnergyVAD()
        assert vad.is_speech(b"", 16000) is False

    def test_short_frame_is_silence(self):
        vad = EnergyVAD()
        assert vad.is_speech(b"\x01", 16000) is False


# ---------------------------------------------------------------------------
# build_vad tests.
# ---------------------------------------------------------------------------
class TestBuildVAD:
    def test_default_returns_energy_vad(self):
        vad = build_vad()
        assert isinstance(vad, EnergyVAD)

    def test_webrtc_fallback_to_energy(self):
        # webrtcvad is not installed in the test env → EnergyVAD.
        vad = build_vad(use_webrtc=True)
        assert isinstance(vad, EnergyVAD)


# ---------------------------------------------------------------------------
# STTSession tests.
# ---------------------------------------------------------------------------
class TestSTTSession:
    @pytest.mark.asyncio
    async def test_emits_partial_while_speaking(self):
        transcriber = MockTranscriber(partial_text="hello")
        callback = MockCallback()
        session = STTSession(
            "stream-1",
            EnergyVAD(),
            transcriber,
            callback,
            partial_interval_frames=2,
        )

        # Feed 5 speech frames — should trigger partials.
        frame = _speech_frame()
        for _ in range(5):
            await session.feed_pcm(frame)

        partials = [e for e in callback.events if e["event_type"] == "audio:transcript_partial"]
        assert len(partials) >= 1
        assert partials[0]["transcript"] == "hello"

    @pytest.mark.asyncio
    async def test_emits_final_on_trailing_silence(self):
        transcriber = MockTranscriber(final_text="hello world")
        callback = MockCallback()
        session = STTSession(
            "stream-1",
            EnergyVAD(),
            transcriber,
            callback,
            partial_interval_frames=100,  # avoid partials
            silence_frames_to_final=3,
        )

        # Feed speech then silence.
        await session.feed_pcm(_speech_frame())
        await session.feed_pcm(_silence_frame())
        await session.feed_pcm(_silence_frame())
        await session.feed_pcm(_silence_frame())  # triggers final

        finals = [e for e in callback.events if e["event_type"] == "audio:transcript_final"]
        assert len(finals) == 1
        assert finals[0]["transcript"] == "hello world"
        assert session.finalized

    @pytest.mark.asyncio
    async def test_finalize_emits_final_on_audio_stop(self):
        transcriber = MockTranscriber(final_text="final text")
        callback = MockCallback()
        session = STTSession(
            "stream-1",
            EnergyVAD(),
            transcriber,
            callback,
            partial_interval_frames=100,
        )

        # Feed speech without trailing silence.
        await session.feed_pcm(_speech_frame())
        await session.feed_pcm(_speech_frame())

        # audio:stop → finalize.
        await session.finalize(message_id="msg-123")

        finals = [e for e in callback.events if e["event_type"] == "audio:transcript_final"]
        assert len(finals) == 1
        assert finals[0]["transcript"] == "final text"
        assert finals[0]["message_id"] == "msg-123"

    @pytest.mark.asyncio
    async def test_finalize_emits_empty_when_no_speech(self):
        transcriber = MockTranscriber()
        callback = MockCallback()
        session = STTSession(
            "stream-1",
            EnergyVAD(),
            transcriber,
            callback,
        )

        # Feed only silence.
        await session.feed_pcm(_silence_frame())
        await session.finalize()

        finals = [e for e in callback.events if e["event_type"] == "audio:transcript_final"]
        assert len(finals) == 1
        assert finals[0]["transcript"] == ""

    @pytest.mark.asyncio
    async def test_stt_failure_emits_ticket_error(self):
        transcriber = MockTranscriber(fail=True)
        callback = MockCallback()
        session = STTSession(
            "stream-1",
            EnergyVAD(),
            transcriber,
            callback,
            silence_frames_to_final=2,
        )

        # Feed speech then silence to trigger final → failure.
        await session.feed_pcm(_speech_frame())
        await session.feed_pcm(_silence_frame())
        await session.feed_pcm(_silence_frame())

        # Should have emitted a ticket:error (STT_UNAVAILABLE).
        errors = [e for e in callback.events if e["event_type"] == "ticket:error"]
        assert len(errors) == 1
        assert session.finalized

    @pytest.mark.asyncio
    async def test_feeds_after_finalize_are_ignored(self):
        transcriber = MockTranscriber()
        callback = MockCallback()
        session = STTSession(
            "stream-1",
            EnergyVAD(),
            transcriber,
            callback,
            silence_frames_to_final=2,
        )

        await session.feed_pcm(_speech_frame())
        await session.feed_pcm(_silence_frame())
        await session.feed_pcm(_silence_frame())  # finalizes
        assert session.finalized

        # Further frames should be ignored.
        await session.feed_pcm(_speech_frame())
        assert len(transcriber.calls) == 1  # no new transcribe calls

    @pytest.mark.asyncio
    async def test_partial_not_emitted_when_unchanged(self):
        transcriber = MockTranscriber(partial_text="same")
        callback = MockCallback()
        session = STTSession(
            "stream-1",
            EnergyVAD(),
            transcriber,
            callback,
            partial_interval_frames=1,
        )

        # Feed speech — first partial emitted, subsequent identical ones suppressed.
        await session.feed_pcm(_speech_frame())
        await session.feed_pcm(_speech_frame())
        await session.feed_pcm(_speech_frame())

        partials = [e for e in callback.events if e["event_type"] == "audio:transcript_partial"]
        assert len(partials) == 1  # deduplicated


# ---------------------------------------------------------------------------
# STTService tests.
# ---------------------------------------------------------------------------
class TestSTTService:
    @pytest.mark.asyncio
    async def test_start_feed_stop_lifecycle(self):
        transcriber = MockTranscriber(final_text="hello")
        service = STTService(transcriber=transcriber)
        callback = MockCallback()

        session = service.start_stream("s1", callback)
        assert session is not None
        assert service.get_session("s1") is session

        # Feed a speech frame then stop.
        await service.feed_pcm("s1", _speech_frame())
        await service.stop_stream("s1", message_id="m1")

        # Session removed after stop.
        assert service.get_session("s1") is None
        # Final transcript emitted.
        finals = [e for e in callback.events if e["event_type"] == "audio:transcript_final"]
        assert len(finals) == 1

    @pytest.mark.asyncio
    async def test_feed_unknown_stream_drops_frame(self):
        service = STTService(transcriber=MockTranscriber())
        # Should not raise.
        await service.feed_pcm("unknown", _speech_frame())

    @pytest.mark.asyncio
    async def test_stop_unknown_stream_is_noop(self):
        service = STTService(transcriber=MockTranscriber())
        await service.stop_stream("unknown")  # should not raise

    @pytest.mark.asyncio
    async def test_start_replaces_existing_session(self):
        service = STTService(transcriber=MockTranscriber())
        cb1 = MockCallback()
        cb2 = MockCallback()

        service.start_stream("s1", cb1)
        service.start_stream("s1", cb2)

        # The second session replaced the first.
        assert service.get_session("s1") is not None

    @pytest.mark.asyncio
    async def test_health_returns_true_when_transcriber_loads(self):
        class HealthyTranscriber:
            def load(self):
                pass

            async def transcribe(self, audio, sr, *, partial=False):
                return ("", True)

        service = STTService(transcriber=HealthyTranscriber())
        assert await service.health() is True

    @pytest.mark.asyncio
    async def test_health_returns_false_on_load_failure(self):
        class BrokenTranscriber:
            def load(self):
                raise RuntimeError("model missing")

            async def transcribe(self, audio, sr, *, partial=False):
                return ("", True)

        service = STTService(transcriber=BrokenTranscriber())
        assert await service.health() is False


# ---------------------------------------------------------------------------
# AudioDecoder tests.
# ---------------------------------------------------------------------------
class TestAudioDecoder:
    def test_passthrough_16k_mono(self):
        pcm = b"\x00\x01" * 480
        result = AudioDecoder.resample_to_16k_mono(pcm, 16000, 1)
        assert result == pcm

    def test_downmix_stereo_to_mono(self):
        # 2-channel frame: left=silence, right=loud.
        import struct as _struct

        stereo = bytearray()
        for _ in range(480):
            stereo += _struct.pack("<hh", 0, 10000)
        result = AudioDecoder.resample_to_16k_mono(bytes(stereo), 16000, 2)
        # Mono should have half the bytes (960 bytes = 480 samples * 2 bytes).
        assert len(result) == 960

    def test_resample_from_8k_to_16k(self):
        # 8 kHz mono frame (240 samples = 480 bytes) → 16 kHz doubles length.
        pcm_8k = _speech_frame(5000)[:480]  # 240 samples at 8 kHz
        result = AudioDecoder.resample_to_16k_mono(pcm_8k, 8000, 1)
        assert len(result) > len(pcm_8k)


# ---------------------------------------------------------------------------
# Integration: VAD → transcribe → callback (no ARQ).
# ---------------------------------------------------------------------------
class TestSTTIntegration:
    @pytest.mark.asyncio
    async def test_full_flow_no_arq_enqueue(self):
        """Verify audio frames go through the callback, never ARQ.

        The callback records events; there is no Redis/ARQ interaction
        in the STT path — only the final transcript triggers ARQ
        dispatch (in the Gateway callback, not here).
        """
        transcriber = MockTranscriber(
            partial_text="hello", final_text="hello world"
        )
        callback = MockCallback()
        service = STTService(transcriber=transcriber)

        service.start_stream("s1", callback)
        await service.feed_pcm("s1", _speech_frame())
        await service.feed_pcm("s1", _speech_frame())
        await service.stop_stream("s1", message_id="msg-1")

        # Events were emitted through the callback only.
        assert len(callback.events) > 0
        types = {e["event_type"] for e in callback.events}
        assert "audio:transcript_final" in types
