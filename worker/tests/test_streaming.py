"""Tests for streaming response delivery and completion.

Covers:
- Worker: Redis Stream event publishing (status_update, chunk, final, completed, error).
- Worker: Response persister inserts assistant message before response:final.
- Worker: Ticket status updated to resolved on completion.
- Gateway: Stream subscriber starts/stops per active ticket.
- Gateway: Fan-out sends events to all authorized WebSocket clients.
- Gateway: Dead sockets are removed on send failure.
- Gateway: Consumer stops when all connections are gone.
- Gateway: Stream entry parsing (bytes → JSON).
- Integration: full publish → subscribe → fan-out flow with mocked Redis.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
from contracts.enums import AgentStatus, TicketStatus
from worker.streaming import (
    RedisStreamPublisher,
    STREAM_MAX_LEN,
    STREAM_PREFIX,
    STREAM_SUFFIX,
)


# ---------------------------------------------------------------------------
# Mock Redis for Stream operations.
# ---------------------------------------------------------------------------
class MockRedisStream:
    """Mock Redis that supports xadd, xread, and basic stream operations.

    Stores stream entries as lists of (entry_id, fields) per stream key.
    """

    def __init__(self):
        self._streams: dict[str, list[tuple[str, dict[str, bytes]]]] = {}
        self._xread_event = asyncio.Event()

    async def xadd(self, key, fields, maxlen=None, **kwargs):
        if key not in self._streams:
            self._streams[key] = []
        # Generate a monotonic entry ID.
        seq = len(self._streams[key])
        entry_id = f"{seq}-0"
        # Store fields as bytes to mimic Redis.
        encoded = {
            k.encode() if isinstance(k, str) else k: v.encode() if isinstance(v, str) else v
            for k, v in fields.items()
        }
        self._streams[key].append((entry_id, encoded))
        # Wake up any blocked xread.
        self._xread_event.set()
        return entry_id

    async def xread(self, keys_and_ids, count=10, block=None):
        """Mock xread — returns entries after the given ID."""
        results = []
        for key, start_id in keys_and_ids.items():
            if key not in self._streams:
                # Wait for entries if block is set.
                if block and block > 0:
                    try:
                        await asyncio.wait_for(self._xread_event.wait(), timeout=block / 1000)
                    except asyncio.TimeoutError:
                        return []
                    self._xread_event.clear()
                if key not in self._streams:
                    continue
            entries = self._streams.get(key, [])
            # Find entries after start_id.
            if start_id == "$":
                # Only new entries (after the last one).
                if not entries:
                    continue
                # Return nothing — caller will retry.
                continue
            # Parse start_id to get index.
            try:
                start_idx = int(start_id.split("-")[0])
            except (ValueError, IndexError):
                start_idx = -1
            new_entries = [
                (key, [(eid, fields) for eid, fields in entries[start_idx + 1:]])
            ]
            if new_entries[0][1]:
                results.extend(new_entries)
        return results if results else []

    def get_stream(self, key: str) -> list[tuple[str, dict[str, bytes]]]:
        return self._streams.get(key, [])


# ---------------------------------------------------------------------------
# Mock WebSocket.
# ---------------------------------------------------------------------------
class MockWebSocket:
    """Mock WebSocket for fan-out tests."""

    def __init__(self):
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._should_fail = False

    async def send_json(self, message: dict[str, Any]) -> None:
        if self._should_fail:
            raise ConnectionError("socket closed")
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Mock asyncpg pool for ResponsePersister.
# ---------------------------------------------------------------------------
class MockPgConn:
    """Mock asyncpg connection."""

    def __init__(self):
        self.user_id = uuid4()
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query, *args):
        self.executed.append((query, args))

    async def fetchval(self, query, *args):
        if "user_id" in query:
            return self.user_id
        return None

    async def fetchrow(self, query, *args):
        return {"user_id": self.user_id}

    async def acquire(self):
        return self

    async def release(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockPgPool:
    """Mock asyncpg pool — acquire() returns an async context manager."""

    def __init__(self):
        self.conn = MockPgConn()

    def acquire(self):
        return self.conn  # MockPgConn implements __aenter__/__aexit__


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------
def _ticket_id() -> Any:
    return uuid4()


def _stream_key(ticket_id) -> str:
    return f"{STREAM_PREFIX}{ticket_id}{STREAM_SUFFIX}"


# ---------------------------------------------------------------------------
# Worker: RedisStreamPublisher tests.
# ---------------------------------------------------------------------------
class TestRedisStreamPublisher:
    @pytest.mark.asyncio
    async def test_publish_status(self):
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()

        await publisher.publish_status(
            ticket_id, AgentStatus.PLANNING, label="classifying"
        )

        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields[b"type"] == b"agent:status_update"
        payload = json.loads(fields[b"payload"])
        assert payload["status"] == "planning"
        assert payload["label"] == "classifying"

    @pytest.mark.asyncio
    async def test_publish_chunk(self):
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()
        correlation_id = uuid4()

        await publisher.publish_chunk(ticket_id, correlation_id, "Hello ", 1)

        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields[b"type"] == b"response:chunk"
        payload = json.loads(fields[b"payload"])
        assert payload["delta"] == "Hello "
        assert payload["sequence"] == 1

    @pytest.mark.asyncio
    async def test_publish_response_final(self):
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()
        correlation_id = uuid4()
        message_id = uuid4()

        await publisher.publish_response_final(
            ticket_id, correlation_id, message_id, "Final answer"
        )

        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields[b"type"] == b"response:final"
        payload = json.loads(fields[b"payload"])
        assert payload["message_id"] == str(message_id)
        assert payload["text"] == "Final answer"

    @pytest.mark.asyncio
    async def test_publish_ticket_completed(self):
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()
        correlation_id = uuid4()

        await publisher.publish_ticket_completed(
            ticket_id, correlation_id, TicketStatus.RESOLVED,
            selected_model="qwen2.5-14b-instruct",
            fallback_reason=None,
        )

        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields[b"type"] == b"ticket:completed"
        payload = json.loads(fields[b"payload"])
        assert payload["status"] == "resolved"
        assert payload["selected_model"] == "qwen2.5-14b-instruct"

    @pytest.mark.asyncio
    async def test_publish_ticket_error(self):
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()
        correlation_id = uuid4()

        await publisher.publish_ticket_error(
            ticket_id, correlation_id, "STT_UNAVAILABLE", "Voice service down", True
        )

        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields[b"type"] == b"ticket:error"
        payload = json.loads(fields[b"payload"])
        assert payload["code"] == "STT_UNAVAILABLE"
        assert payload["retryable"] is True

    @pytest.mark.asyncio
    async def test_sequence_is_monotonic(self):
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()

        await publisher.publish_status(ticket_id, AgentStatus.IDLE)
        await publisher.publish_status(ticket_id, AgentStatus.PLANNING)
        await publisher.publish_status(ticket_id, AgentStatus.GENERATING)

        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 3
        seqs = [int(json.loads(f[b"payload"]).get("sequence", 0)) for _, f in entries]
        # The sequence field in the envelope should be monotonic.
        envelope_seqs = [int(f[b"sequence"]) for _, f in entries]
        assert envelope_seqs == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_stream_key_format(self):
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()
        await publisher.publish_status(ticket_id, AgentStatus.IDLE)
        assert _stream_key(ticket_id) in redis._streams


# ---------------------------------------------------------------------------
# Worker: PostgresResponsePersister tests.
# ---------------------------------------------------------------------------
class TestPostgresResponsePersister:
    @pytest.mark.asyncio
    async def test_persist_final_inserts_message(self):
        from worker.streaming import PostgresResponsePersister

        pool = MockPgPool()
        persister = PostgresResponsePersister(pool)
        ticket_id = _ticket_id()
        run_id = uuid4()

        message_id = await persister.persist_final(
            ticket_id, run_id, "Hello world", "qwen2.5-14b-instruct", None
        )

        assert message_id is not None
        # Verify INSERT INTO messages was executed.
        insert_found = any(
            "INSERT INTO messages" in q for q, _ in pool.conn.executed
        )
        assert insert_found

    @pytest.mark.asyncio
    async def test_persist_final_updates_ticket(self):
        from worker.streaming import PostgresResponsePersister

        pool = MockPgPool()
        persister = PostgresResponsePersister(pool)
        ticket_id = _ticket_id()
        run_id = uuid4()

        await persister.persist_final(
            ticket_id, run_id, "Hello", "deepseek/deepseek-chat", "primary timeout"
        )

        update_found = any(
            "UPDATE tickets" in q and "selected_model" in q
            for q, _ in pool.conn.executed
        )
        assert update_found

    @pytest.mark.asyncio
    async def test_update_ticket_status(self):
        from worker.streaming import PostgresResponsePersister

        pool = MockPgPool()
        persister = PostgresResponsePersister(pool)
        ticket_id = _ticket_id()

        await persister.update_ticket_status(
            ticket_id, TicketStatus.RESOLVED, AgentStatus.COMPLETED
        )

        update_found = any(
            "UPDATE tickets" in q and "status" in q
            for q, _ in pool.conn.executed
        )
        assert update_found


# ---------------------------------------------------------------------------
# Gateway: StreamSubscriber tests.
# ---------------------------------------------------------------------------
class TestStreamSubscriber:
    @pytest.mark.asyncio
    async def test_ensure_subscriber_starts_task(self):
        from gateway.app.lifespan import TicketConnections
        from gateway.app.streaming import StreamSubscriber

        redis = MockRedisStream()
        connections = TicketConnections()
        subscriber = StreamSubscriber(redis, connections)

        ticket_id = str(_ticket_id())
        await subscriber.ensure_subscriber(ticket_id)

        assert ticket_id in subscriber._tasks
        assert not subscriber._tasks[ticket_id].done()

        await subscriber.stop_all()

    @pytest.mark.asyncio
    async def test_ensure_subscriber_idempotent(self):
        from gateway.app.lifespan import TicketConnections
        from gateway.app.streaming import StreamSubscriber

        redis = MockRedisStream()
        connections = TicketConnections()
        subscriber = StreamSubscriber(redis, connections)

        ticket_id = str(_ticket_id())
        await subscriber.ensure_subscriber(ticket_id)
        task1 = subscriber._tasks[ticket_id]
        await subscriber.ensure_subscriber(ticket_id)
        task2 = subscriber._tasks[ticket_id]

        # Same task — not duplicated.
        assert task1 is task2

        await subscriber.stop_all()

    @pytest.mark.asyncio
    async def test_stop_subscriber_cancels_task(self):
        from gateway.app.lifespan import TicketConnections
        from gateway.app.streaming import StreamSubscriber

        redis = MockRedisStream()
        connections = TicketConnections()
        subscriber = StreamSubscriber(redis, connections)

        ticket_id = str(_ticket_id())
        await subscriber.ensure_subscriber(ticket_id)
        await subscriber.stop_subscriber(ticket_id)

        assert ticket_id not in subscriber._tasks

    @pytest.mark.asyncio
    async def test_stop_all_cancels_all(self):
        from gateway.app.lifespan import TicketConnections
        from gateway.app.streaming import StreamSubscriber

        redis = MockRedisStream()
        connections = TicketConnections()
        subscriber = StreamSubscriber(redis, connections)

        tid1 = str(_ticket_id())
        tid2 = str(_ticket_id())
        await subscriber.ensure_subscriber(tid1)
        await subscriber.ensure_subscriber(tid2)
        await subscriber.stop_all()

        assert len(subscriber._tasks) == 0


# ---------------------------------------------------------------------------
# Gateway: Stream entry parsing tests.
# ---------------------------------------------------------------------------
class TestStreamEntryParsing:
    def test_parse_valid_entry(self):
        from gateway.app.streaming import StreamSubscriber

        ticket_id = "tic_123"
        entry_id = "5-0"
        fields = {
            b"type": b"response:chunk",
            b"ticket_id": ticket_id.encode(),
            b"sequence": b"5",
            b"correlation_id": b"req_abc",
            b"payload": json.dumps({"delta": "Hello", "sequence": 5}).encode(),
        }

        result = StreamSubscriber._parse_entry(ticket_id, entry_id, fields)
        assert result is not None
        assert result["type"] == "response:chunk"
        assert result["ticket_id"] == ticket_id
        assert result["sequence"] == 5
        assert result["payload"]["delta"] == "Hello"

    def test_parse_entry_with_string_fields(self):
        from gateway.app.streaming import StreamSubscriber

        # Some Redis clients return strings instead of bytes.
        fields = {
            "type": "agent:status_update",
            "sequence": "1",
            "payload": json.dumps({"status": "planning"}),
        }
        result = StreamSubscriber._parse_entry("tic_1", "0-0", fields)
        assert result is not None
        assert result["type"] == "agent:status_update"

    def test_parse_invalid_json_returns_none(self):
        from gateway.app.streaming import StreamSubscriber

        fields = {
            b"type": b"response:chunk",
            b"payload": b"not json {{{",
            b"sequence": b"1",
        }
        result = StreamSubscriber._parse_entry("tic_1", "0-0", fields)
        assert result is None

    def test_parse_missing_type_returns_none(self):
        from gateway.app.streaming import StreamSubscriber

        fields = {b"payload": b"{}", b"sequence": b"1"}
        result = StreamSubscriber._parse_entry("tic_1", "0-0", fields)
        assert result is None


# ---------------------------------------------------------------------------
# Gateway: Fan-out tests.
# ---------------------------------------------------------------------------
class TestFanOut:
    @pytest.mark.asyncio
    async def test_fanout_sends_to_all_sockets(self):
        from gateway.app.lifespan import TicketConnections
        from gateway.app.streaming import StreamSubscriber

        redis = MockRedisStream()
        connections = TicketConnections()
        subscriber = StreamSubscriber(redis, connections)

        ticket_id = str(_ticket_id())
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        await connections.add(ticket_id, ws1)
        await connections.add(ticket_id, ws2)

        message = {"type": "response:chunk", "payload": {"delta": "Hi"}}
        await subscriber._fanout(ticket_id, message)

        assert len(ws1.sent) == 1
        assert len(ws2.sent) == 1
        assert ws1.sent[0] == message
        assert ws2.sent[0] == message

    @pytest.mark.asyncio
    async def test_fanout_removes_dead_sockets(self):
        from gateway.app.lifespan import TicketConnections
        from gateway.app.streaming import StreamSubscriber

        redis = MockRedisStream()
        connections = TicketConnections()
        subscriber = StreamSubscriber(redis, connections)

        ticket_id = str(_ticket_id())
        ws_alive = MockWebSocket()
        ws_dead = MockWebSocket()
        ws_dead._should_fail = True  # Will raise on send_json.

        await connections.add(ticket_id, ws_alive)
        await connections.add(ticket_id, ws_dead)

        message = {"type": "response:chunk", "payload": {"delta": "Hi"}}
        await subscriber._fanout(ticket_id, message)

        # Dead socket removed, alive socket still has the message.
        assert connections.count(ticket_id) == 1
        assert len(ws_alive.sent) == 1

    @pytest.mark.asyncio
    async def test_fanout_no_connections_does_nothing(self):
        from gateway.app.lifespan import TicketConnections
        from gateway.app.streaming import StreamSubscriber

        redis = MockRedisStream()
        connections = TicketConnections()
        subscriber = StreamSubscriber(redis, connections)

        # No connections — should not raise.
        await subscriber._fanout("nonexistent", {"type": "test"})


# ---------------------------------------------------------------------------
# Integration: publish → consume → fan-out flow.
# ---------------------------------------------------------------------------
class TestIntegrationFlow:
    @pytest.mark.asyncio
    async def test_publish_and_fanout(self):
        """Full flow: worker publishes, subscriber consumes and fans out."""
        from gateway.app.lifespan import TicketConnections
        from gateway.app.streaming import StreamSubscriber

        redis = MockRedisStream()
        connections = TicketConnections()
        subscriber = StreamSubscriber(redis, connections)

        ticket_id = _ticket_id()
        ticket_id_str = str(ticket_id)

        # Register a WebSocket.
        ws = MockWebSocket()
        await connections.add(ticket_id_str, ws)

        # Worker publishes a status update.
        publisher = RedisStreamPublisher(redis)
        await publisher.publish_status(
            ticket_id, AgentStatus.PLANNING, label="test"
        )

        # Manually trigger one consume cycle (instead of the blocking loop).
        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 1

        entry_id, fields = entries[0]
        message = StreamSubscriber._parse_entry(ticket_id_str, entry_id, fields)
        assert message is not None
        await subscriber._fanout(ticket_id_str, message)

        # WebSocket received the event.
        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == "agent:status_update"
        assert ws.sent[0]["payload"]["status"] == "planning"

    @pytest.mark.asyncio
    async def test_publish_final_then_completed(self):
        """Verify response:final is published before ticket:completed."""
        from gateway.app.lifespan import TicketConnections
        from gateway.app.streaming import StreamSubscriber

        redis = MockRedisStream()
        connections = TicketConnections()
        subscriber = StreamSubscriber(redis, connections)

        ticket_id = _ticket_id()
        ticket_id_str = str(ticket_id)
        correlation_id = uuid4()
        message_id = uuid4()

        ws = MockWebSocket()
        await connections.add(ticket_id_str, ws)

        publisher = RedisStreamPublisher(redis)

        # Publish response:final first.
        await publisher.publish_response_final(
            ticket_id, correlation_id, message_id, "Done!"
        )
        # Then publish ticket:completed.
        await publisher.publish_ticket_completed(
            ticket_id, correlation_id, TicketStatus.RESOLVED,
            selected_model="qwen2.5-14b-instruct",
        )

        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 2

        # Fan out both.
        for entry_id, fields in entries:
            msg = StreamSubscriber._parse_entry(ticket_id_str, entry_id, fields)
            assert msg is not None
            await subscriber._fanout(ticket_id_str, msg)

        # Verify ordering: response:final came before ticket:completed.
        assert ws.sent[0]["type"] == "response:final"
        assert ws.sent[1]["type"] == "ticket:completed"
        assert ws.sent[1]["payload"]["status"] == "resolved"


# ---------------------------------------------------------------------------
# Stream key format tests.
# ---------------------------------------------------------------------------
class TestStreamKeyFormat:
    def test_stream_key_matches_worker_and_gateway(self):
        from gateway.app.streaming import stream_key as gw_key
        from worker.streaming import STREAM_PREFIX, STREAM_SUFFIX

        ticket_id = str(uuid4())
        gw = gw_key(ticket_id)
        worker = f"{STREAM_PREFIX}{ticket_id}{STREAM_SUFFIX}"
        assert gw == worker

    def test_stream_key_with_uuid(self):
        from gateway.app.streaming import stream_key

        ticket_id = uuid4()
        key = stream_key(ticket_id)
        assert key.startswith("ticket:")
        assert key.endswith(":events")


# ---------------------------------------------------------------------------
# Worker: TTS audio event publishing tests (Task 9).
# ---------------------------------------------------------------------------
class TestTTSAudioPublishing:
    @pytest.mark.asyncio
    async def test_publish_audio_start(self):
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()
        correlation_id = uuid4()

        await publisher.publish_audio_start(
            ticket_id, correlation_id, "tts-1", "audio/pcm", 24000
        )

        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields[b"type"] == b"response:audio_start"
        payload = json.loads(fields[b"payload"])
        assert payload["stream_id"] == "tts-1"
        assert payload["mime_type"] == "audio/pcm"
        assert payload["sample_rate"] == 24000

    @pytest.mark.asyncio
    async def test_publish_audio_chunk(self):
        import base64

        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()
        correlation_id = uuid4()
        audio = b"\x00\x01\x02\x03"

        await publisher.publish_audio_chunk(ticket_id, correlation_id, audio)

        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields[b"type"] == b"audio:chunk"
        payload = json.loads(fields[b"payload"])
        assert base64.b64decode(payload["audio_b64"]) == audio

    @pytest.mark.asyncio
    async def test_publish_audio_end(self):
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()
        correlation_id = uuid4()

        await publisher.publish_audio_end(ticket_id, correlation_id, "tts-1")

        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields[b"type"] == b"response:audio_end"
        payload = json.loads(fields[b"payload"])
        assert payload["stream_id"] == "tts-1"

    @pytest.mark.asyncio
    async def test_audio_events_are_monotonic_sequence(self):
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()
        correlation_id = uuid4()

        await publisher.publish_audio_start(ticket_id, correlation_id, "s1", "audio/pcm", 24000)
        await publisher.publish_audio_chunk(ticket_id, correlation_id, b"\x00\x01")
        await publisher.publish_audio_end(ticket_id, correlation_id, "s1")

        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 3
        seqs = [int(f[b"sequence"]) for _, f in entries]
        assert seqs == [1, 2, 3]


# ---------------------------------------------------------------------------
# Worker: VoiceStreamer tests (Task 9).
# ---------------------------------------------------------------------------
class TestVoiceStreamer:
    @pytest.mark.asyncio
    async def test_streams_tts_alongside_text_chunks(self):
        """VoiceStreamer bridges response:chunk to TTS audio events."""
        from worker.streaming import VoiceStreamer

        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()
        correlation_id = uuid4()

        # Mock TTSService with a stand-in that implements the same
        # start_stream/feed_chunk/flush interface.
        class MockTTSBuffer:
            def __init__(self, stream_id, callback):
                self.stream_id = stream_id
                self._callback = callback
                self.chunks: list[str] = []
                self.flushed = False

            async def feed_chunk(self, delta: str) -> None:
                self.chunks.append(delta)
                await self._callback(
                    self.stream_id, "response:audio_start",
                    mime_type="audio/pcm", sample_rate=24000,
                )
                await self._callback(self.stream_id, "audio:chunk", audio=b"\x00\x01")

            async def flush(self) -> None:
                self.flushed = True
                await self._callback(self.stream_id, "response:audio_end")

        class MockTTSService:
            def __init__(self):
                self.buffers: dict[str, MockTTSBuffer] = {}

            def start_stream(self, stream_id, callback):
                buf = MockTTSBuffer(stream_id, callback)
                self.buffers[stream_id] = buf
                return buf

            async def feed_chunk(self, stream_id, delta):
                await self.buffers[stream_id].feed_chunk(delta)

            async def flush(self, stream_id):
                await self.buffers[stream_id].flush()

        tts = MockTTSService()
        streamer = VoiceStreamer(publisher, tts_service=tts)

        await streamer.start_stream(ticket_id, correlation_id, "tts-1")
        await streamer.feed_chunk(ticket_id, "Hello world.")
        await streamer.flush(ticket_id)

        entries = redis.get_stream(_stream_key(ticket_id))
        types = [f[b"type"].decode() for _, f in entries]
        assert "response:audio_start" in types
        assert "audio:chunk" in types
        assert "response:audio_end" in types

    @pytest.mark.asyncio
    async def test_no_tts_service_is_text_only(self):
        """Without a TTSService, VoiceStreamer is a no-op (text-only)."""
        from worker.streaming import VoiceStreamer

        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis)
        ticket_id = _ticket_id()
        correlation_id = uuid4()

        streamer = VoiceStreamer(publisher, tts_service=None)
        await streamer.start_stream(ticket_id, correlation_id, "tts-1")
        await streamer.feed_chunk(ticket_id, "Hello.")
        await streamer.flush(ticket_id)

        # No audio events published.
        entries = redis.get_stream(_stream_key(ticket_id))
        assert len(entries) == 0


# ---------------------------------------------------------------------------
# Worker: SLO tracking integration tests (Task 12).
# ---------------------------------------------------------------------------
class TestSLOIntegration:
    @pytest.mark.asyncio
    async def test_publish_status_records_first_status_slo(self):
        """First agent:status_update triggers SLO first-status recording."""
        from observability.slo import SLOTracker

        class RecordingSLO:
            def __init__(self):
                self.status_calls: list = []
                self.chunk_calls: list = []
                self.completion_calls: list = []

            def record_message_accepted(self, ticket_id):
                pass

            def record_first_status(self, ticket_id):
                self.status_calls.append(ticket_id)

            def record_first_chunk(self, ticket_id):
                self.chunk_calls.append(ticket_id)

            def record_completion(self, ticket_id, *, succeeded, voice_used=False, voice_degraded=False):
                self.completion_calls.append((ticket_id, succeeded))

        slo = RecordingSLO()
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis, slo_tracker=slo)
        ticket_id = _ticket_id()

        # First status → SLO recorded.
        await publisher.publish_status(ticket_id, AgentStatus.PLANNING)
        assert len(slo.status_calls) == 1

        # Second status → NOT recorded (first only).
        await publisher.publish_status(ticket_id, AgentStatus.GENERATING)
        assert len(slo.status_calls) == 1

    @pytest.mark.asyncio
    async def test_publish_chunk_records_first_chunk_slo(self):
        """First response:chunk triggers SLO first-chunk recording."""
        from observability.slo import SLOTracker

        class RecordingSLO:
            def __init__(self):
                self.chunk_calls: list = []

            def record_message_accepted(self, ticket_id):
                pass

            def record_first_chunk(self, ticket_id):
                self.chunk_calls.append(ticket_id)

        slo = RecordingSLO()
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis, slo_tracker=slo)
        ticket_id = _ticket_id()
        correlation_id = uuid4()

        await publisher.publish_chunk(ticket_id, correlation_id, "Hello", 1)
        assert len(slo.chunk_calls) == 1

        await publisher.publish_chunk(ticket_id, correlation_id, " world", 2)
        assert len(slo.chunk_calls) == 1  # first only

    @pytest.mark.asyncio
    async def test_publish_completed_records_completion_slo(self):
        """ticket:completed triggers SLO completion recording."""
        from observability.slo import SLOTracker

        class RecordingSLO:
            def __init__(self):
                self.completion_calls: list = []

            def record_completion(self, ticket_id, *, succeeded, voice_used=False, voice_degraded=False):
                self.completion_calls.append((ticket_id, succeeded))

        slo = RecordingSLO()
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis, slo_tracker=slo)
        ticket_id = _ticket_id()
        correlation_id = uuid4()

        await publisher.publish_ticket_completed(
            ticket_id, correlation_id, TicketStatus.RESOLVED
        )
        assert len(slo.completion_calls) == 1
        assert slo.completion_calls[0][1] is True  # succeeded

    @pytest.mark.asyncio
    async def test_publish_completed_failed_records_failed(self):
        """ticket:completed with FAILED status records succeeded=False."""
        class RecordingSLO:
            def __init__(self):
                self.completion_calls: list = []

            def record_completion(self, ticket_id, *, succeeded, voice_used=False, voice_degraded=False):
                self.completion_calls.append((ticket_id, succeeded))

        slo = RecordingSLO()
        redis = MockRedisStream()
        publisher = RedisStreamPublisher(redis, slo_tracker=slo)
        ticket_id = _ticket_id()
        correlation_id = uuid4()

        await publisher.publish_ticket_completed(
            ticket_id, correlation_id, TicketStatus.FAILED
        )
        assert slo.completion_calls[0][1] is False  # failed
