"""Application lifespan: shared asyncpg pool, Redis, and socket registry.

Per `AgenticProject.md` §2 (Gateway lifespan and connection management):
- asyncpg pool (min 5, max 25, command_timeout 15)
- redis.asyncio connection
- `TicketConnections` registry for active WebSocket fan-out
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, WebSocket
from redis.asyncio import Redis

from gateway.app.config import settings


MAX_TICKET_WS_CONNECTIONS = 5


class TicketConnections:
    """In-memory registry of active WebSocket connections per ticket.

    The Gateway is stateless except for this registry. PostgreSQL remains the
    replay source of truth; this map only tracks live sockets for fan-out.
    """

    def __init__(self) -> None:
        self._sockets: dict[str, list[WebSocket]] = defaultdict(list)

    async def add(self, ticket_id: str, ws: WebSocket) -> None:
        """Register a socket for a ticket.

        Reject a 6th concurrent socket for the same ticket.
        """
        sockets = self._sockets[ticket_id]
        if len(sockets) >= MAX_TICKET_WS_CONNECTIONS:
            raise RuntimeError("too many active connections")
        sockets.append(ws)

    def remove(self, ticket_id: str, ws: WebSocket) -> None:
        """Deregister a socket; clean up empty lists."""
        sockets = self._sockets.get(ticket_id)
        if not sockets:
            return
        try:
            sockets.remove(ws)
        except ValueError:
            pass
        if not sockets:
            self._sockets.pop(ticket_id, None)

    def count(self, ticket_id: str) -> int:
        """Number of active sockets for a ticket."""
        return len(self._sockets.get(ticket_id, []))

    def get(self, ticket_id: str) -> list[WebSocket]:
        """Return the list of sockets for a ticket."""
        return list(self._sockets.get(ticket_id, []))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create shared resources on startup, tear down on shutdown."""
    app.state.pg = await asyncpg.create_pool(
        dsn=settings.database_dsn,
        min_size=settings.pg_min_size,
        max_size=settings.pg_max_size,
        command_timeout=settings.pg_command_timeout,
    )
    app.state.redis = Redis.from_url(
        settings.redis_url,
        decode_responses=False,
    )
    app.state.connections = TicketConnections()

    # ARQ pool for enqueuing ticket runs (reused across requests so jobs
    # aren't dropped when a per-request pool is closed prematurely).
    from arq.connections import RedisSettings, create_pool

    app.state.arq_pool = await create_pool(
        RedisSettings.from_dsn(settings.arq_redis_url)
    )

    # Stream subscriber for Redis Stream fan-out (Task 10).
    from gateway.app.streaming import StreamSubscriber

    app.state.stream_subscriber = StreamSubscriber(
        app.state.redis, app.state.connections
    )

    # Observability (Task 12).  Tracer, metrics, and SLO tracker are
    # injected so tests can mock them.  Production wiring uses real
    # OpenTelemetry / Prometheus implementations.  Built before the voice
    # services so STT/TTS latency can be recorded through the SLO tracker.
    from observability import (
        NoopMetrics,
        NoopTracer,
        SLOTracker,
        build_metrics,
        build_tracer,
        configure_logging,
    )

    configure_logging()
    app.state.tracer = build_tracer(service_name="gateway")
    app.state.metrics = build_metrics()
    app.state.slo_tracker = SLOTracker(metrics=app.state.metrics)

    # Voice services (Task 9).  Lazily constructed so the Gateway boots
    # even when faster_whisper / kokoro native deps are absent — the
    # health endpoint reports them as unhealthy and the audio path emits
    # STT_UNAVAILABLE / falls back to text.  The SLO tracker is injected
    # so STT/TTS latency is exported to Prometheus.
    from voice.stt import STTService

    app.state.stt_service = STTService(slo_tracker=app.state.slo_tracker)

    # TTS service (Task 9).  Lazily constructed like STT — the health
    # endpoint reports unavailable if Kokoro can't load, and the worker
    # completes tickets with text only.
    from voice.tts import TTSService

    app.state.tts_service = TTSService(slo_tracker=app.state.slo_tracker)

    yield

    await app.state.stream_subscriber.stop_all()
    await app.state.arq_pool.aclose()
    await app.state.redis.aclose()
    await app.state.pg.close()
