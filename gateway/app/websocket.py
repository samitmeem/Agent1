"""WebSocket support for ticket connections and event replay."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from jose import JWTError, jwt
from redis.asyncio import Redis

from contracts.events import (
    AudioStartPayload,
    AudioStopPayload,
    AudioStreamPayload,
    MessageSubmitPayload,
    TicketConnectPayload,
    validate_event,
)
from gateway.app.config import settings
from gateway.app.lifespan import TicketConnections
from observability import set_correlation_id, set_request_id, set_ticket_id
from voice.stt import AudioDecoder, STTError, STTService

logger = logging.getLogger("gateway.websocket")

router = APIRouter()


def _extract_token_from_ws(websocket: WebSocket) -> str | None:
    token = websocket.cookies.get("access_token")
    if token:
        return token
    protocol = websocket.headers.get("sec-websocket-protocol")
    if protocol:
        return protocol.split(",")[0].strip()
    return None


def _decode_jwt(token: str) -> UUID:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        subject = payload.get("sub")
        if not subject:
            raise ValueError("missing sub claim")
        return UUID(subject)
    except (JWTError, ValueError) as exc:
        raise ValueError("invalid token") from exc


async def _send_json(ws: WebSocket, message: dict[str, Any]) -> None:
    try:
        await ws.send_json(message)
    except Exception as exc:
        logger.debug("send_json failed, closing websocket: %s", exc)
        await ws.close(code=status.WS_1011_INTERNAL_ERROR)


async def _broadcast(ticket_id: str, connections: TicketConnections, message: dict[str, Any]) -> None:
    sockets = connections.get(ticket_id)
    for ws in sockets:
        await _send_json(ws, message)


def _update_active_connections_gauge(app_state: Any) -> None:
    """Refresh the ``ws_active_connections`` gauge after connect/disconnect.

    The gauge reflects the total number of live WebSocket sockets across
    all tickets — a key Prometheus metric for capacity planning.
    """
    metrics = getattr(app_state, "metrics", None)
    if metrics is None:
        return
    connections: TicketConnections = app_state.connections
    total = sum(len(sockets) for sockets in connections._sockets.values())
    metrics.set_gauge("ws_active_connections", total)


async def _load_ticket_owner(conn: asyncpg.Connection, ticket_id: UUID) -> dict[str, Any] | None:
    return await conn.fetchrow(
        """
        SELECT id, user_id, status, agent_status, last_event_sequence
        FROM tickets
        WHERE id = $1
        """,
        ticket_id,
    )


async def _replay_events(conn: asyncpg.Connection, ticket_id: UUID, last_event_id: str | None) -> list[dict[str, Any]]:
    sequence_offset = 0
    if last_event_id is not None:
        row = await conn.fetchrow(
            "SELECT sequence FROM events WHERE id = $1 AND ticket_id = $2",
            UUID(last_event_id),
            ticket_id,
        )
        if row is not None:
            sequence_offset = row["sequence"]

    rows = await conn.fetch(
        """
        SELECT id, type, ticket_id, sequence, correlation_id, payload
        FROM events
        WHERE ticket_id = $1 AND sequence > $2
        ORDER BY sequence ASC
        """,
        ticket_id,
        sequence_offset,
    )
    return [
        {
            "event_id": str(row["id"]),
            "type": row["type"],
            "ticket_id": str(row["ticket_id"]),
            "sequence": row["sequence"],
            "correlation_id": str(row["correlation_id"]) if row["correlation_id"] else None,
            "payload": row["payload"],
        }
        for row in rows
    ]


async def _insert_message_and_outbox(
    conn: asyncpg.Connection,
    ticket_id: UUID,
    user_id: UUID,
    payload: MessageSubmitPayload,
    correlation_id: UUID,
) -> tuple[UUID, bool]:
    message_id = uuid4()
    outbox_id = uuid4()
    event_id = uuid4()

    # CRITICAL: All 4 operations wrapped in a transaction to prevent orphaned
    # data if the process crashes mid-flight (message without sequence, event,
    # or outbox entry).
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO messages (id, ticket_id, user_id, role, text, client_message_id)
            VALUES ($1, $2, $3, 'user', $4, $5)
            """,
            message_id,
            ticket_id,
            user_id,
            payload.text,
            UUID(payload.client_message_id),
        )

        sequence = await conn.fetchval(
            """
            UPDATE tickets
            SET last_event_sequence = last_event_sequence + 1
            WHERE id = $1
            RETURNING last_event_sequence
            """,
            ticket_id,
        )

        await conn.execute(
            """
            INSERT INTO events (id, ticket_id, sequence, type, correlation_id, payload)
            VALUES ($1, $2, $3, 'message:accepted', $4, $5)
            """,
            event_id,
            ticket_id,
            sequence,
            correlation_id,
            json.dumps({
                "message_id": str(message_id),
                "client_message_id": payload.client_message_id,
                "queued": True,
            }),
        )

        await conn.execute(
            """
            INSERT INTO outbox_events (id, ticket_id, message_id, event_type, payload, dispatched)
            VALUES ($1, $2, $3, 'ticket.run_requested', $4, false)
            """,
            outbox_id,
            ticket_id,
            message_id,
            json.dumps({
                "message_id": str(message_id),
                "ticket_id": str(ticket_id),
                "correlation_id": str(correlation_id),
            }),
        )

    return message_id, True


async def _get_existing_message_id(
    conn: asyncpg.Connection,
    ticket_id: UUID,
    client_message_id: UUID,
) -> UUID | None:
    row = await conn.fetchrow(
        "SELECT id FROM messages WHERE ticket_id = $1 AND client_message_id = $2",
        ticket_id,
        client_message_id,
    )
    return row["id"] if row else None


async def _dispatch_arq(arq_pool: Any, ticket_id: UUID, message_id: UUID, correlation_id: UUID) -> None:
    """Enqueue a ticket run via the shared ARQ pool.

    The pool is created once at gateway startup (``app.state.arq_pool``) and
    reused across requests so jobs aren't dropped when a per-request pool is
    closed prematurely.  The ``run_ticket`` function is registered in
    ``worker.entrypoint.WorkerSettings.functions``.
    """
    await arq_pool.enqueue_job(
        "run_ticket",
        str(ticket_id),
        str(message_id),
        str(correlation_id),
    )


async def _persist_transcript_and_dispatch(
    conn: asyncpg.Connection,
    arq_pool: Any,
    ticket_id: UUID,
    user_id: UUID,
    transcript: str,
    stream_id: str,
    correlation_id: UUID,
) -> UUID:
    """Persist a final voice transcript as a user message and dispatch ARQ.

    Called by the STT transcript callback when ``audio:transcript_final``
    arrives.  The transcript is stored exactly like a text ``message:submit``
    — same outbox + ARQ dispatch path — so the worker processes voice and
    text identically.
    """
    message_id = uuid4()
    outbox_id = uuid4()
    event_id = uuid4()
    client_message_id = f"voice:{stream_id}"

    await conn.execute(
        """
        INSERT INTO messages (id, ticket_id, user_id, role, text, client_message_id)
        VALUES ($1, $2, $3, 'user', $4, $5)
        """,
        message_id,
        ticket_id,
        user_id,
        transcript,
        client_message_id,
    )

    sequence = await conn.fetchval(
        """
        UPDATE tickets
        SET last_event_sequence = last_event_sequence + 1
        WHERE id = $1
        RETURNING last_event_sequence
        """,
        ticket_id,
    )

    await conn.execute(
        """
        INSERT INTO events (id, ticket_id, sequence, type, correlation_id, payload)
        VALUES ($1, $2, $3, 'audio:transcript_final', $4, $5)
        """,
        event_id,
        ticket_id,
        sequence,
        correlation_id,
        json.dumps({"stream_id": stream_id, "transcript": transcript, "message_id": str(message_id)}),
    )

    await conn.execute(
        """
        INSERT INTO outbox_events (id, ticket_id, message_id, event_type, payload, dispatched)
        VALUES ($1, $2, $3, 'ticket.run_requested', $4, false)
        """,
        outbox_id,
        ticket_id,
        message_id,
        json.dumps({"message_id": str(message_id), "ticket_id": str(ticket_id), "correlation_id": str(correlation_id)}),
    )

    await _dispatch_arq(arq_pool, ticket_id, message_id, correlation_id)
    return message_id


def _make_transcript_callback(
    websocket: WebSocket,
    ticket_id: UUID,
    connections: TicketConnections,
    redis: Redis,
    pg_pool: asyncpg.Pool,
    user_id: UUID,
):
    """Build the async callback the STT session invokes for transcript events.

    - ``audio:transcript_partial`` → broadcast to all ticket sockets.
    - ``audio:transcript_final`` → persist as a user message, dispatch ARQ,
      and broadcast the final event.
    - ``ticket:error`` (STT failure) → broadcast a ``ticket:error`` with
      ``code="STT_UNAVAILABLE"`` so the client can fall back to text.
    """

    async def _callback(
        stream_id: str,
        event_type: str,
        transcript: str,
        message_id: str | None = None,
    ) -> None:
        correlation_id = uuid4()
        if event_type == "audio:transcript_partial":
            await _broadcast(
                str(ticket_id),
                connections,
                {
                    "type": "audio:transcript_partial",
                    "ticket_id": str(ticket_id),
                    "correlation_id": str(correlation_id),
                    "payload": {"stream_id": stream_id, "transcript": transcript},
                },
            )
        elif event_type == "audio:transcript_final":
            # Persist the transcript and dispatch to the worker.
            if transcript.strip():
                async with pg_pool.acquire() as conn:
                    persisted_id = await _persist_transcript_and_dispatch(
                        conn,
                        websocket.app.state.arq_pool,
                        ticket_id,
                        user_id,
                        transcript,
                        stream_id,
                        correlation_id,
                    )
                    msg_id = str(persisted_id)
            else:
                msg_id = message_id or ""
            await _broadcast(
                str(ticket_id),
                connections,
                {
                    "type": "audio:transcript_final",
                    "ticket_id": str(ticket_id),
                    "correlation_id": str(correlation_id),
                    "payload": {
                        "stream_id": stream_id,
                        "transcript": transcript,
                        "message_id": msg_id,
                    },
                },
            )
        elif event_type == "ticket:error":
            # STT failure — emit STT_UNAVAILABLE, preserve ticket context.
            await _broadcast(
                str(ticket_id),
                connections,
                {
                    "type": "ticket:error",
                    "ticket_id": str(ticket_id),
                    "correlation_id": str(correlation_id),
                    "payload": {
                        "code": "STT_UNAVAILABLE",
                        "message": "Voice transcription is unavailable. Please use text input.",
                        "retryable": False,
                    },
                },
            )

    return _callback


async def _heartbeat_loop(websocket: WebSocket, pong_received: asyncio.Event) -> None:
    """Send periodic pings and close the socket after 2 missed pongs."""
    missed_pongs = 0
    while True:
        await asyncio.sleep(20)
        try:
            await websocket.send_json({"type": "ping", "payload": {"timestamp": asyncio.get_event_loop().time()}})
        except Exception:
            break

        try:
            await asyncio.wait_for(pong_received.wait(), timeout=20)
            missed_pongs = 0
        except asyncio.TimeoutError:
            missed_pongs += 1
            if missed_pongs >= 2:
                await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
                break
        finally:
            pong_received.clear()


@router.websocket("/ws/tickets/{ticket_id}")
async def ticket_socket(websocket: WebSocket, ticket_id: UUID) -> None:
    token = _extract_token_from_ws(websocket)
    if token is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    try:
        user_id = _decode_jwt(token)
    except ValueError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept(subprotocol=token)
    connections: TicketConnections = websocket.app.state.connections
    redis: Redis = websocket.app.state.redis

    async with websocket.app.state.pg.acquire() as conn:
        ticket = await _load_ticket_owner(conn, ticket_id)
        if ticket is None or ticket["user_id"] != user_id:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        try:
            await connections.add(str(ticket_id), websocket)
        except RuntimeError:
            await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
            return
        _update_active_connections_gauge(websocket.app.state)

        # Start the Redis Stream consumer for this ticket (Task 10).
        stream_subscriber = websocket.app.state.stream_subscriber
        await stream_subscriber.ensure_subscriber(str(ticket_id))

        ping_event = asyncio.Event()
        heartbeat_task = asyncio.create_task(_heartbeat_loop(websocket, ping_event))

        try:
            raw = await websocket.receive_json()
            if raw.get("type") != "ticket:connect":
                await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                return
            payload = validate_event("ticket:connect", raw.get("payload", {}))
            assert isinstance(payload, TicketConnectPayload)

            events = await _replay_events(conn, ticket_id, payload.last_event_id)
            ticket_snapshot = {
                "id": str(ticket["id"]),
                "user_id": str(ticket["user_id"]),
                "status": ticket["status"],
                "agent_status": ticket["agent_status"],
                "last_event_sequence": ticket["last_event_sequence"],
            }

            await websocket.send_json(
                {
                    "type": "ticket:connected",
                    "ticket_id": str(ticket_id),
                    "correlation_id": str(uuid4()),
                    "payload": {
                        "ticket_snapshot": ticket_snapshot,
                        "replay_complete": True,
                        "last_event_id": events[-1]["event_id"] if events else payload.last_event_id,
                    },
                }
            )

            for event in events:
                await websocket.send_json(event)

            ping_outstanding = False
            missed_pongs = 0
            while True:
                try:
                    raw_message = await asyncio.wait_for(websocket.receive_json(), timeout=20)
                except asyncio.TimeoutError:
                    if not ping_outstanding:
                        await websocket.send_json(
                            {
                                "type": "ping",
                                "ticket_id": str(ticket_id),
                                "correlation_id": str(uuid4()),
                                "payload": {"timestamp": asyncio.get_event_loop().time()},
                            }
                        )
                        ping_outstanding = True
                    else:
                        missed_pongs += 1
                        if missed_pongs >= 2:
                            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
                            return
                    continue

                event_type = raw_message.get("type")
                payload_data = raw_message.get("payload", {})

                if event_type == "message:submit":
                    payload = validate_event("message:submit", payload_data)
                    assert isinstance(payload, MessageSubmitPayload)
                    correlation_id = UUID(raw_message.get("correlation_id", str(uuid4())))

                    # Observability: set logging context + start ack timer.
                    set_correlation_id(str(correlation_id))
                    set_ticket_id(str(ticket_id))
                    ack_start = time.monotonic()

                    try:
                        async with conn.transaction():
                            try:
                                message_id, queued = await _insert_message_and_outbox(
                                    conn,
                                    ticket_id,
                                    user_id,
                                    payload,
                                    correlation_id,
                                )
                            except asyncpg.UniqueViolationError:
                                existing_id = await _get_existing_message_id(
                                    conn,
                                    ticket_id,
                                    UUID(payload.client_message_id),
                                )
                                if existing_id is None:
                                    raise
                                message_id = existing_id
                                queued = False

                        if queued:
                            await _dispatch_arq(websocket.app.state.arq_pool, ticket_id, message_id, correlation_id)

                        accepted_payload = {
                            "message_id": str(message_id),
                            "client_message_id": payload.client_message_id,
                            "queued": queued,
                        }
                        await _broadcast(
                            str(ticket_id),
                            connections,
                            {
                                "type": "message:accepted",
                                "ticket_id": str(ticket_id),
                                "correlation_id": str(correlation_id),
                                "payload": accepted_payload,
                            },
                        )

                        # Observability: record ack latency + SLO.
                        ack_latency_ms = (time.monotonic() - ack_start) * 1000
                        slo_tracker = getattr(websocket.app.state, "slo_tracker", None)
                        if slo_tracker is not None:
                            slo_tracker.record_ack_latency(ticket_id, ack_latency_ms)
                            slo_tracker.record_message_accepted(ticket_id)
                        metrics = getattr(websocket.app.state, "metrics", None)
                        if metrics is not None:
                            metrics.inc_counter(
                                "ticket_messages_accepted_total",
                                labels={"queued": "true" if queued else "false"},
                            )
                    except Exception:
                        logger.exception("message submit failed")
                        await websocket.send_json(
                            {
                                "type": "ticket:error",
                                "ticket_id": str(ticket_id),
                                "correlation_id": str(correlation_id),
                                "payload": {
                                    "code": "MESSAGE_SUBMIT_FAILED",
                                    "message": "Could not submit message.",
                                    "retryable": True,
                                },
                            }
                        )
                elif event_type == "pong":
                    ping_outstanding = False
                    missed_pongs = 0
                    continue
                elif event_type == "ping":
                    await websocket.send_json(
                        {
                            "type": "pong",
                            "ticket_id": str(ticket_id),
                            "correlation_id": str(uuid4()),
                            "payload": {"timestamp": asyncio.get_event_loop().time()},
                        }
                    )
                elif event_type == "audio:start":
                    payload = validate_event("audio:start", payload_data)
                    assert isinstance(payload, AudioStartPayload)
                    stt_service: STTService = websocket.app.state.stt_service
                    callback = _make_transcript_callback(
                        websocket,
                        ticket_id,
                        connections,
                        redis,
                        websocket.app.state.pg,
                        user_id,
                    )
                    stt_service.start_stream(payload.stream_id, callback)
                elif event_type == "audio:stream":
                    payload = validate_event("audio:stream", payload_data)
                    assert isinstance(payload, AudioStreamPayload)
                    # The binary frame follows this metadata event.
                    try:
                        binary = await asyncio.wait_for(
                            websocket.receive_bytes(), timeout=10
                        )
                    except asyncio.TimeoutError:
                        await websocket.send_json(
                            {
                                "type": "ticket:error",
                                "ticket_id": str(ticket_id),
                                "correlation_id": str(uuid4()),
                                "payload": {
                                    "code": "AUDIO_TIMEOUT",
                                    "message": "Audio frame not received.",
                                    "retryable": True,
                                },
                            }
                        )
                        continue
                    stt_service = websocket.app.state.stt_service
                    # Decode to 16 kHz mono PCM (browser sends opus/webm;
                    # in production a codec library handles Opus→PCM, then
                    # AudioDecoder resamples).  For raw-PCM test frames the
                    # decode is a passthrough.
                    try:
                        pcm = AudioDecoder.resample_to_16k_mono(
                            binary, sample_rate=16000, in_channels=1
                        )
                        await stt_service.feed_pcm(payload.stream_id, pcm)
                    except STTError:
                        # STT failure already emitted via the callback.
                        logger.warning("STT error processing frame for %s", payload.stream_id)
                elif event_type == "audio:stop":
                    payload = validate_event("audio:stop", payload_data)
                    assert isinstance(payload, AudioStopPayload)
                    stt_service = websocket.app.state.stt_service
                    await stt_service.stop_stream(payload.stream_id)
                else:
                    await websocket.send_json(
                        {
                            "type": "ticket:error",
                            "ticket_id": str(ticket_id),
                            "correlation_id": str(uuid4()),
                            "payload": {
                                "code": "UNSUPPORTED_EVENT",
                                "message": f"Unsupported event type: {event_type}",
                                "retryable": False,
                            },
                        }
                    )
        except WebSocketDisconnect:
            return
        finally:
            heartbeat_task.cancel()
            connections.remove(str(ticket_id), websocket)
            _update_active_connections_gauge(websocket.app.state)
            # Stop the stream consumer if no more connections for this ticket.
            if connections.count(str(ticket_id)) == 0:
                await stream_subscriber.stop_subscriber(str(ticket_id))
