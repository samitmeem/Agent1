"""Worker-side streaming: Redis event publisher and response persister.

Implements the ``EventPublisherProtocol`` and ``ResponsePersisterProtocol``
from ``worker.graph``.  The worker publishes events to a Redis Stream
named ``ticket:{ticket_id}:events``.  The Gateway subscribes to this stream
and fans events out to authorized WebSocket clients.

Key ordering guarantee: the assistant message is persisted to PostgreSQL
**before** ``response:final`` is published, so a client that receives
``response:final`` can safely assume the message is durable.

Observability (Task 12): the publisher optionally accepts an
``SLOTracker`` and ``MetricsRegistry`` to record first-status and
first-chunk latencies, LLM/tool counters, and completion metrics.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import UUID, uuid4

from redis.asyncio import Redis

from contracts.enums import AgentStatus, TicketStatus

logger = logging.getLogger("worker.streaming")

# Redis Stream key prefix.
STREAM_PREFIX = "ticket:"
STREAM_SUFFIX = ":events"

# Maximum stream length (keeps memory bounded — old entries are trimmed).
STREAM_MAX_LEN = 1000


# ---------------------------------------------------------------------------
# EventEnvelope — the wire format for Redis Stream entries.
# ---------------------------------------------------------------------------
def _build_envelope(
    event_type: str,
    ticket_id: UUID,
    correlation_id: UUID,
    payload: dict[str, Any],
    sequence: int = 0,
) -> dict[str, str]:
    """Build a Redis Stream entry (all values must be str).

    Returns a flat dict suitable for ``redis.xadd``.
    """
    envelope = {
        "type": event_type,
        "ticket_id": str(ticket_id),
        "sequence": str(sequence),
        "correlation_id": str(correlation_id),
        "payload": json.dumps(payload),
        "sent_at": str(time.time()),
    }
    return envelope


# ---------------------------------------------------------------------------
# RedisStreamPublisher — implements EventPublisherProtocol.
# ---------------------------------------------------------------------------
class RedisStreamPublisher:
    """Publishes ``agent:status_update`` and ``response:chunk`` events.

    Satisfies ``worker.graph.EventPublisherProtocol``.  Events are written
    to a Redis Stream named ``ticket:{ticket_id}:events``.  The Gateway
    subscribes to this stream and fans events to WebSocket clients.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        slo_tracker: Any | None = None,
        metrics: Any | None = None,
    ) -> None:
        self._redis = redis
        self._slo = slo_tracker
        self._metrics = metrics
        # Per-ticket sequence counter (monotonic within a run).
        self._sequences: dict[UUID, int] = {}
        # Track first status/chunk per ticket for SLO measurement.
        self._first_status_sent: set[UUID] = set()
        self._first_chunk_sent: set[UUID] = set()

    def _stream_key(self, ticket_id: UUID) -> str:
        return f"{STREAM_PREFIX}{ticket_id}{STREAM_SUFFIX}"

    def _next_sequence(self, ticket_id: UUID) -> int:
        seq = self._sequences.get(ticket_id, 0) + 1
        self._sequences[ticket_id] = seq
        return seq

    async def publish_status(
        self,
        ticket_id: UUID,
        status: AgentStatus,
        label: str | None = None,
        tool_name: str | None = None,
        detail: str | None = None,
        correlation_id: UUID | None = None,
    ) -> None:
        """Publish an ``agent:status_update`` event to the Redis Stream.

        ``correlation_id`` is propagated from the graph state so the trace
        context spans Gateway → Redis Stream → Worker → LLM → Tools.
        When omitted, a fresh id is generated (backward compatible).
        """
        payload: dict[str, Any] = {"status": str(status)}
        if label:
            payload["label"] = label
        if tool_name:
            payload["tool_name"] = tool_name
        if detail:
            payload["detail"] = detail

        correlation = correlation_id or uuid4()
        seq = self._next_sequence(ticket_id)
        envelope = _build_envelope(
            "agent:status_update", ticket_id, correlation, payload, seq
        )
        await self._redis.xadd(
            self._stream_key(ticket_id), envelope, maxlen=STREAM_MAX_LEN, approximate=True
        )
        logger.debug(
            "published agent:status_update to %s seq=%d status=%s",
            self._stream_key(ticket_id),
            seq,
            status,
        )
        # Observability: record first-status SLO (once per ticket run).
        if self._slo is not None and ticket_id not in self._first_status_sent:
            self._first_status_sent.add(ticket_id)
            self._slo.record_first_status(ticket_id)
        if self._metrics is not None:
            self._metrics.inc_counter(
                "agent_status_events_total",
                labels={"status": str(status)},
            )

    async def publish_chunk(
        self,
        ticket_id: UUID,
        correlation_id: UUID,
        delta: str,
        sequence: int,
    ) -> None:
        """Publish a ``response:chunk`` event (text delta) to the stream."""
        payload = {"sequence": sequence, "delta": delta}
        envelope = _build_envelope(
            "response:chunk", ticket_id, correlation_id, payload, sequence
        )
        await self._redis.xadd(
            self._stream_key(ticket_id), envelope, maxlen=STREAM_MAX_LEN, approximate=True
        )
        # Observability: record first-chunk SLO (once per ticket run).
        if self._slo is not None and ticket_id not in self._first_chunk_sent:
            self._first_chunk_sent.add(ticket_id)
            self._slo.record_first_chunk(ticket_id)

    async def publish_response_final(
        self,
        ticket_id: UUID,
        correlation_id: UUID,
        message_id: UUID,
        text: str,
        citations: list[dict] | None = None,
        actions: list[dict] | None = None,
    ) -> None:
        """Publish ``response:final`` — must be called AFTER the assistant
        message is persisted to PostgreSQL.
        """
        payload: dict[str, Any] = {
            "message_id": str(message_id),
            "text": text,
            "citations": citations or [],
            "actions": actions or [],
        }
        seq = self._next_sequence(ticket_id)
        envelope = _build_envelope(
            "response:final", ticket_id, correlation_id, payload, seq
        )
        await self._redis.xadd(
            self._stream_key(ticket_id), envelope, maxlen=STREAM_MAX_LEN, approximate=True
        )
        logger.info(
            "published response:final to %s message_id=%s",
            self._stream_key(ticket_id),
            message_id,
        )

    async def publish_ticket_completed(
        self,
        ticket_id: UUID,
        correlation_id: UUID,
        status: TicketStatus,
        selected_model: str | None = None,
        fallback_reason: str | None = None,
    ) -> None:
        """Publish ``ticket:completed`` with final status and model info."""
        payload: dict[str, Any] = {"status": str(status)}
        if selected_model:
            payload["selected_model"] = selected_model
        if fallback_reason:
            payload["fallback_reason"] = fallback_reason

        seq = self._next_sequence(ticket_id)
        envelope = _build_envelope(
            "ticket:completed", ticket_id, correlation_id, payload, seq
        )
        await self._redis.xadd(
            self._stream_key(ticket_id), envelope, maxlen=STREAM_MAX_LEN, approximate=True
        )
        logger.info(
            "published ticket:completed to %s status=%s",
            self._stream_key(ticket_id),
            status,
        )
        # Observability: record completion SLO.
        if self._slo is not None:
            self._slo.record_completion(
                ticket_id,
                succeeded=(status == TicketStatus.RESOLVED),
            )
        # Clean up per-ticket SLO tracking state.
        self._first_status_sent.discard(ticket_id)
        self._first_chunk_sent.discard(ticket_id)

    async def publish_ticket_error(
        self,
        ticket_id: UUID,
        correlation_id: UUID,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> None:
        """Publish a ``ticket:error`` event."""
        payload = {"code": code, "message": message, "retryable": retryable}
        seq = self._next_sequence(ticket_id)
        envelope = _build_envelope(
            "ticket:error", ticket_id, correlation_id, payload, seq
        )
        await self._redis.xadd(
            self._stream_key(ticket_id), envelope, maxlen=STREAM_MAX_LEN, approximate=True
        )

    async def publish_audio_start(
        self,
        ticket_id: UUID,
        correlation_id: UUID,
        stream_id: str,
        mime_type: str,
        sample_rate: int,
    ) -> None:
        """Publish ``response:audio_start`` — precedes binary TTS frames.

        Per §1 step 11: each sentence's audio is delivered as binary
        WebSocket frames preceded by a ``response:audio_start`` envelope.
        """
        payload = {
            "stream_id": stream_id,
            "mime_type": mime_type,
            "sample_rate": sample_rate,
        }
        seq = self._next_sequence(ticket_id)
        envelope = _build_envelope(
            "response:audio_start", ticket_id, correlation_id, payload, seq
        )
        await self._redis.xadd(
            self._stream_key(ticket_id), envelope, maxlen=STREAM_MAX_LEN, approximate=True
        )

    async def publish_audio_chunk(
        self,
        ticket_id: UUID,
        correlation_id: UUID,
        audio: bytes,
    ) -> None:
        """Publish a binary audio chunk (TTS frame) to the stream.

        Audio frames are stored as base64 in the Redis Stream so the
        Gateway can decode and forward them as binary WebSocket frames.
        Audio is never enqueued in ARQ — only in the event stream.
        """
        import base64

        payload = {"audio_b64": base64.b64encode(audio).decode("ascii")}
        seq = self._next_sequence(ticket_id)
        envelope = _build_envelope(
            "audio:chunk", ticket_id, correlation_id, payload, seq
        )
        await self._redis.xadd(
            self._stream_key(ticket_id), envelope, maxlen=STREAM_MAX_LEN, approximate=True
        )

    async def publish_audio_end(
        self,
        ticket_id: UUID,
        correlation_id: UUID,
        stream_id: str,
    ) -> None:
        """Publish ``response:audio_end`` — TTS stream complete."""
        payload = {"stream_id": stream_id}
        seq = self._next_sequence(ticket_id)
        envelope = _build_envelope(
            "response:audio_end", ticket_id, correlation_id, payload, seq
        )
        await self._redis.xadd(
            self._stream_key(ticket_id), envelope, maxlen=STREAM_MAX_LEN, approximate=True
        )


# ---------------------------------------------------------------------------
# ResponsePersister — implements ResponsePersisterProtocol.
# ---------------------------------------------------------------------------
class PostgresResponsePersister:
    """Persists the final assistant message and updates ticket status.

    Satisfies ``worker.graph.ResponsePersisterProtocol``.  The assistant
    message is inserted into the ``messages`` table **before**
    ``response:final`` is published, guaranteeing durability.

    Uses ``asyncpg.Pool`` for PostgreSQL access.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def persist_final(
        self,
        ticket_id: UUID,
        run_id: UUID,
        response_text: str,
        selected_model: str | None,
        fallback_reason: str | None,
    ) -> UUID:
        """Insert the assistant message and update ticket status.

        Returns the ``message_id`` of the persisted message.
        """
        message_id = uuid4()
        async with self._pool.acquire() as conn:
            # Insert the assistant message.
            await conn.execute(
                """
                INSERT INTO messages (id, ticket_id, user_id, role, text, model)
                VALUES ($1, $2, $3, 'assistant', $4, $5)
                """,
                message_id,
                ticket_id,
                # user_id is on the ticket row — fetch it.
                (await conn.fetchval(
                    "SELECT user_id FROM tickets WHERE id = $1", ticket_id
                )),
                response_text,
                selected_model,
            )

            # Update ticket with model/fallback info.
            await conn.execute(
                """
                UPDATE tickets
                SET selected_model = $1,
                    fallback_reason = $2,
                    updated_at = NOW()
                WHERE id = $3
                """,
                selected_model,
                fallback_reason,
                ticket_id,
            )

        logger.info(
            "persisted assistant message %s for ticket %s",
            message_id,
            ticket_id,
        )
        return message_id

    async def update_ticket_status(
        self,
        ticket_id: UUID,
        status: TicketStatus,
        agent_status: AgentStatus,
    ) -> None:
        """Update the ticket's status and agent_status."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tickets
                SET status = $1, agent_status = $2, updated_at = NOW()
                WHERE id = $3
                """,
                str(status),
                str(agent_status),
                ticket_id,
            )


# ---------------------------------------------------------------------------
# VoiceStreamer — bridges response:chunk events to TTS audio.
# ---------------------------------------------------------------------------
class VoiceStreamer:
    """Streams TTS audio alongside text chunks for voice-enabled tickets.

    Per §1 step 11: the worker buffers ``response:chunk`` text by sentence
    boundary, synthesizes each sentence via Kokoro, and publishes
    ``response:audio_start`` + binary ``audio:chunk`` + ``response:audio_end``
    events to the Redis Stream.  Audio frames go directly to the stream
    (never ARQ).

    On TTS failure, the streamer logs and continues — the ticket completes
    with text only (graceful degradation per §3).
    """

    def __init__(
        self,
        publisher: RedisStreamPublisher,
        tts_service: Any | None = None,
    ) -> None:
        self._publisher = publisher
        self._tts_service = tts_service
        self._buffers: dict[UUID, Any] = {}  # ticket_id -> TTSStreamBuffer

    def _make_callback(self, ticket_id: UUID, correlation_id: UUID, stream_id: str):
        """Build the AudioFrameCallback that publishes audio events."""
        publisher = self._publisher

        async def _callback(
            sid: str,
            event_type: str,
            audio: bytes | None = None,
            mime_type: str | None = None,
            sample_rate: int | None = None,
        ) -> None:
            if event_type == "response:audio_start":
                await publisher.publish_audio_start(
                    ticket_id, correlation_id, sid, mime_type or "audio/pcm", sample_rate or 24000
                )
            elif event_type == "audio:chunk" and audio:
                await publisher.publish_audio_chunk(ticket_id, correlation_id, audio)
            elif event_type == "response:audio_end":
                await publisher.publish_audio_end(ticket_id, correlation_id, sid)

        return _callback

    async def start_stream(
        self,
        ticket_id: UUID,
        correlation_id: UUID,
        stream_id: str,
    ) -> None:
        """Start a TTS buffer for a voice-enabled ticket response."""
        if self._tts_service is None:
            return  # TTS not configured — text-only mode.
        callback = self._make_callback(ticket_id, correlation_id, stream_id)
        buffer = self._tts_service.start_stream(stream_id, callback)
        self._buffers[ticket_id] = buffer

    async def feed_chunk(self, ticket_id: UUID, delta: str) -> None:
        """Feed a text delta to the TTS buffer for sentence-boundary synthesis."""
        buffer = self._buffers.get(ticket_id)
        if buffer is None:
            return
        await buffer.feed_chunk(delta)

    async def flush(self, ticket_id: UUID) -> None:
        """Flush remaining TTS buffer and emit response:audio_end."""
        buffer = self._buffers.pop(ticket_id, None)
        if buffer is None:
            return
        await buffer.flush()
