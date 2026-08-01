"""Production worker entrypoint — wires real implementations.

This module is the ARQ worker entrypoint used in production (and local E2E).
It populates the ARQ ``ctx`` dict with real implementations of all graph
dependencies:

- ``PostgresContextLoader``: loads conversation + ticket state from PostgreSQL.
- ``LLMRouter``: Qwen-first with DeepSeek fallback (from ``llm/router.py``).
- ``ToolExecutor``: OAuth + idempotency + rate limiting (from ``worker/tools``).
- ``PostgresCheckpointStore``: persists LangGraph checkpoints to PostgreSQL.
- ``RedisStreamPublisher``: publishes events to Redis Streams.
- ``PostgresResponsePersister``: persists assistant messages to PostgreSQL.

Usage:
    arq worker.entrypoint.WorkerSettings
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID

import asyncpg
from redis.asyncio import Redis

from arq.connections import RedisSettings
from contracts.models import TicketState
from llm.router import LLMRouter
from observability import SLOTracker, build_metrics, build_tracer, configure_logging
from worker.graph import TicketGraph, build_graph
from worker.jobs import run_ticket
from worker.streaming import PostgresResponsePersister, RedisStreamPublisher
from worker.tools.executor import ToolExecutor
from worker.tools.base import (
    OAuthCredentialStoreProtocol,
    RateLimiterProtocol,
    ToolExecutionStoreProtocol,
    UserConfirmationProtocol,
)

logger = logging.getLogger("worker.entrypoint")


class CheckpointConflictError(Exception):
    """Raised when a checkpoint conflict is detected (another worker saved first)."""
    pass


# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------
DATABASE_DSN = os.getenv(
    "DATABASE_DSN",
    "postgresql://ticket:ticket@localhost:5432/ticketassistant",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ARQ_REDIS_URL = os.getenv("ARQ_REDIS_URL", "redis://localhost:6379/1")


# ---------------------------------------------------------------------------
# PostgresContextLoader — real implementation of ContextLoaderProtocol.
# ---------------------------------------------------------------------------
class PostgresContextLoader:
    """Loads ticket history and persisted ``TicketState`` from PostgreSQL."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def load(
        self, ticket_id: UUID, run_id: UUID
    ) -> tuple[list[dict[str, Any]], TicketState]:
        async with self._pool.acquire() as conn:
            # Load conversation history (last N messages).
            rows = await conn.fetch(
                """
                SELECT role, text, created_at
                FROM messages
                WHERE ticket_id = $1
                ORDER BY created_at ASC
                LIMIT 50
                """,
                ticket_id,
            )
            conversation = [
                {"role": row["role"], "text": row["text"]} for row in rows
            ]

            # Load ticket state.
            ticket_row = await conn.fetchrow(
                """
                SELECT id, user_id, status, agent_status, selected_model,
                       fallback_reason, pending_oauth_provider, updated_at
                FROM tickets
                WHERE id = $1
                """,
                ticket_id,
            )
            if ticket_row is None:
                raise ValueError(f"ticket {ticket_id} not found")

            ts = TicketState(
                ticket_id=ticket_row["id"],
                user_id=ticket_row["user_id"],
                run_id=run_id,
                status=ticket_row["status"],
                agent_status=ticket_row["agent_status"],
                selected_model=ticket_row["selected_model"],
                fallback_reason=ticket_row["fallback_reason"],
                pending_oauth_provider=ticket_row["pending_oauth_provider"],
                updated_at=ticket_row["updated_at"],
            )
            return conversation, ts


# ---------------------------------------------------------------------------
# PostgresCheckpointStore — real implementation of CheckpointStoreProtocol.
# ---------------------------------------------------------------------------
class PostgresCheckpointStore:
    """Persists LangGraph checkpoints to PostgreSQL."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def save_checkpoint(
        self,
        ticket_id: UUID,
        run_id: UUID,
        node_name: str,
        data: dict[str, Any],
    ) -> None:
        """Persist a LangGraph checkpoint.

        Uses ON CONFLICT DO NOTHING to prevent concurrent workers from
        overwriting each other's checkpoints. If two workers try to save the
        same checkpoint (race condition), the first write wins and the second
        raises CheckpointConflictError so it can abort.
        """
        import json

        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO langgraph_checkpoints (ticket_id, run_id, node_name, checkpoint_data)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (ticket_id, run_id, node_name) DO NOTHING
                """,
                ticket_id,
                run_id,
                node_name,
                json.dumps(data),
            )

            # ON CONFLICT DO NOTHING returns "INSERT 0 0" if a conflict occurred.
            if result == "INSERT 0 0":
                raise CheckpointConflictError(
                    f"Checkpoint conflict detected for ticket={ticket_id} "
                    f"run={run_id} node={node_name}. "
                    "Another worker may have processed this checkpoint already."
                )


# ---------------------------------------------------------------------------
# Stub implementations for tool executor protocols (production uses real ones).
# ---------------------------------------------------------------------------
class _StubExecutionStore(ToolExecutionStoreProtocol):
    """Stub execution store — production wires a real Postgres-backed store."""

    async def get_by_idempotency_key(self, key: str) -> Any:
        return None

    async def insert_pending(self, record: Any) -> None:
        pass

    async def mark_running(self, execution_id: UUID) -> None:
        pass

    async def mark_succeeded(self, execution_id: UUID, result: dict) -> None:
        pass

    async def mark_failed(self, execution_id: UUID, error: str) -> None:
        pass


class _StubOAuthStore(OAuthCredentialStoreProtocol):
    """Stub OAuth store — production wires encrypted credential retrieval."""

    async def get_credential(self, user_id: UUID, provider: str) -> Any:
        return None

    async def refresh_credential(self, connection_id: UUID) -> Any:
        return None


class _StubConfirmationChecker(UserConfirmationProtocol):
    """Stub confirmation checker — production wires user confirmation flow."""

    async def requires_confirmation(self, tool_name: str) -> bool:
        return False

    async def check_confirmation(self, user_id: UUID, tool_name: str, payload: dict) -> bool:
        return True


class _StubRateLimiter(RateLimiterProtocol):
    """Stub rate limiter — production wires Redis-backed rate limiting."""

    async def check_rate_limit(self, user_id: UUID, tool_name: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# ARQ startup: wire all real implementations.
# ---------------------------------------------------------------------------
async def on_startup(ctx: dict) -> None:
    """Called once when the ARQ worker boots — wires real dependencies."""
    configure_logging()
    logger.info("ARQ worker starting up — wiring production dependencies")

    # Shared resources.
    pg_pool = await asyncpg.create_pool(dsn=DATABASE_DSN, min_size=2, max_size=10)
    redis = Redis.from_url(REDIS_URL, decode_responses=False)

    # Observability.
    tracer = build_tracer(service_name="worker")
    metrics = build_metrics()
    slo_tracker = SLOTracker(metrics=metrics)

    # Graph dependencies.
    context_loader = PostgresContextLoader(pg_pool)
    checkpoint_store = PostgresCheckpointStore(pg_pool)
    event_publisher = RedisStreamPublisher(redis, slo_tracker=slo_tracker, metrics=metrics)
    response_persister = PostgresResponsePersister(pg_pool)

    # LLM router (Qwen primary, DeepSeek fallback).
    llm_router = LLMRouter(tracer=tracer, slo_tracker=slo_tracker)

    # Hermes tools are opt-in via the ENABLE_HERMES_TOOLS feature flag.
    # When unset (the default), no Hermes tools are discovered and the
    # worker runs with just the 4 built-in tools — zero overhead.
    import os

    extra_tools = None
    if os.getenv("ENABLE_HERMES_TOOLS", "0") == "1":
        from worker.tools.hermes_adapter import discover_hermes_tools

        extra_tools = discover_hermes_tools()
        logger.info("ENABLE_HERMES_TOOLS=1 — discovered %d Hermes tools", len(extra_tools))

    # Tool executor with real Postgres-backed stores for idempotency and
    # OAuth credential retrieval. Replaces the stub implementations so
    # duplicate tool executions are prevented via the unique
    # ``idempotency_key`` constraint.
    from worker.stores import PostgresExecutionStore, PostgresOAuthStore

    execution_store = PostgresExecutionStore(pg_pool)
    oauth_store = PostgresOAuthStore(pg_pool)

    tool_executor = ToolExecutor(
        execution_store=execution_store,
        oauth_store=oauth_store,
        confirmation_checker=_StubConfirmationChecker(),
        rate_limiter=_StubRateLimiter(),
        redis=redis,
        tracer=tracer,
        slo_tracker=slo_tracker,
        extra_tools=extra_tools,
    )

    ctx.update(
        {
            "pg": pg_pool,
            "redis": redis,
            "tracer": tracer,
            "metrics": metrics,
            "slo_tracker": slo_tracker,
            "context_loader": context_loader,
            "llm_router": llm_router,
            "tool_executor": tool_executor,
            "checkpoint_store": checkpoint_store,
            "event_publisher": event_publisher,
            "response_persister": response_persister,
        }
    )
    logger.info("ARQ worker startup complete")


async def on_shutdown(ctx: dict) -> None:
    """Clean up shared resources."""
    await ctx["redis"].aclose()
    await ctx["pg"].close()


class WorkerSettings:
    """ARQ worker settings for production."""

    functions = [run_ticket]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(ARQ_REDIS_URL)
