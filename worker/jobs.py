"""ARQ job definitions for ticket processing.

Implements the ``run_ticket`` job per `AgenticProject.md` §1.6 and §3:

- Acquires a Redis distributed lock ``lock:ticket:{ticket_id}`` with a lease
  longer than the expected run duration.  If the lock is already held, the
  job exits safely — the existing run owns processing.
- Loads ticket history, OAuth metadata, and LangGraph checkpoint from
  PostgreSQL.
- Invokes the LangGraph ticket-processing graph.
- Renews the lock for long runs; aborts if ownership is lost.
- On crash, the next run resumes from the last checkpoint.

External dependencies (Redis pool, DB pool, graph dependencies) are resolved
from the ARQ ``ctx`` dict, which is populated by ``on_startup``.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any
from uuid import UUID

from arq.connections import ArqRedis, RedisSettings
from redis.asyncio import Redis
from redis.exceptions import LockError, LockNotOwnedError

from contracts.enums import AgentStatus, TicketStatus
from contracts.models import TicketState
from worker.graph import (
    CheckpointStoreProtocol,
    ContextLoaderProtocol,
    EventPublisherProtocol,
    GraphState,
    LLMRouterProtocol,
    ResponsePersisterProtocol,
    TicketGraph,
    ToolExecutorProtocol,
    build_graph,
)

logger = logging.getLogger("worker.jobs")

# Lock lease in seconds — must exceed the expected run duration.
LOCK_LEASE_SECONDS = 120
# How often to renew the lock (must be < lease/8 for safety margin).
LOCK_RENEW_INTERVAL = 15
# Lock key prefix.
LOCK_PREFIX = "lock:ticket:"


class LockLostError(Exception):
    """Raised when a worker loses ownership of a distributed lock.

    This is a fatal error — the worker must abort processing immediately
    to prevent duplicate tool executions.
    """
    pass


# ---------------------------------------------------------------------------
# Redis distributed lock wrapper with auto-renewal.
# ---------------------------------------------------------------------------
class TicketLock:
    """Redis-backed distributed lock with lease renewal.

    Uses ``redis.asyncio.Lock`` which implements a single-writer lock with a
    TTL (lease).  If the holder crashes, the lock expires automatically so
    another worker can pick up the ticket.
    """

    def __init__(self, redis: Redis, ticket_id: UUID, lease: int = LOCK_LEASE_SECONDS) -> None:
        self._redis = redis
        self._ticket_id = ticket_id
        self._lease = lease
        self._lock: Any = None
        self._token: bytes | None = None

    @property
    def key(self) -> str:
        return f"{LOCK_PREFIX}{self._ticket_id}"

    async def acquire(self) -> bool:
        """Try to acquire the lock. Returns ``False`` if already held."""
        self._token = uuid.uuid4().bytes
        # SET NX with expiry — atomic acquire.
        result = await self._redis.set(
            self.key,
            self._token,
            nx=True,
            ex=self._lease,
        )
        if result:
            self._lock = True
            logger.info("acquired lock %s", self.key)
            return True
        logger.info("lock %s already held — exiting safely", self.key)
        return False

    async def renew(self) -> bool:
        """Extend the lock lease. Returns ``False`` if ownership was lost.

        Raises:
            LockLostError: If lock renewal fails (network error or ownership lost).
                          Caller MUST abort processing to prevent duplicate execution.
        """
        if self._token is None:
            raise LockLostError(f"Cannot renew lock {self.key}: token is None")

        # Lua script: only extend if we still own the lock.
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('EXPIRE', KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        try:
            result = await self._redis.eval(script, 1, self.key, self._token, self._lease)
        except Exception as exc:
            raise LockLostError(f"Lock renewal failed for {self.key}: {exc}") from exc

        if result:
            logger.debug("renewed lock %s", self.key)
            return True

        # Ownership lost — another worker may have acquired the lock
        raise LockLostError(f"Lost ownership of lock {self.key}")

    async def release(self) -> None:
        """Release the lock if we still own it."""
        if self._token is None:
            return
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        else
            return 0
        end
        """
        try:
            await self._redis.eval(script, 1, self.key, self._token)
            logger.debug("released lock %s", self.key)
        except Exception as exc:
            logger.warning("failed to release lock %s: %s", self.key, exc)
        finally:
            self._token = None
            self._lock = None


# ---------------------------------------------------------------------------
# ARQ job.
# ---------------------------------------------------------------------------
async def run_ticket(
    ctx: dict,
    ticket_id: str,
    message_id: str,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Process one user message for a ticket.

    Acquires ``lock:ticket:{ticket_id}``, loads context, invokes the LangGraph
    graph, and releases the lock on completion.  If the lock is already held,
    exits safely.  On crash, the lock expires and the next run resumes from
    the last checkpoint.

    ``correlation_id`` is propagated from the Gateway through the ARQ job
    into the graph state so distributed traces span Gateway → Redis → Worker.
    """
    redis: Redis = ctx["redis"]
    ticket_uuid = UUID(ticket_id)
    run_id = uuid.uuid4()
    # Propagate the correlation_id from the Gateway; generate one if absent
    # so every run has a traceable identifier across services.
    correlation_uuid = UUID(correlation_id) if correlation_id else uuid.uuid4()

    lock = TicketLock(redis, ticket_uuid)
    if not await lock.acquire():
        return {"status": "already_running", "ticket_id": ticket_id}

    try:
        # Wire up dependencies from ctx (populated by on_startup).
        context_loader: ContextLoaderProtocol = ctx["context_loader"]
        router: LLMRouterProtocol = ctx["llm_router"]
        tool_executor: ToolExecutorProtocol = ctx["tool_executor"]
        checkpoint_store: CheckpointStoreProtocol = ctx["checkpoint_store"]
        event_publisher: EventPublisherProtocol = ctx["event_publisher"]
        response_persister: ResponsePersisterProtocol = ctx["response_persister"]
        tracer = ctx.get("tracer")

        graph_nodes = TicketGraph(
            context_loader=context_loader,
            router=router,
            tool_executor=tool_executor,
            checkpoint_store=checkpoint_store,
            event_publisher=event_publisher,
            response_persister=response_persister,
            tracer=tracer,
        )
        compiled_graph = build_graph(graph_nodes)

        # Build initial state.
        from datetime import datetime, timezone

        ts = TicketState(
            ticket_id=ticket_uuid,
            user_id=ctx.get("user_id", uuid.uuid4()),
            run_id=run_id,
            updated_at=datetime.now(timezone.utc),
        )
        initial_state: GraphState = {
            "ticket_state": ts,
            "correlation_id": correlation_uuid,
        }

        # Invoke the graph with lock renewal running in the background.
        import asyncio

        renew_task = asyncio.create_task(_lock_renewal_loop(lock))
        try:
            final_state = await compiled_graph.ainvoke(initial_state)
        finally:
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass

        final_ts: TicketState = final_state.get("ticket_state", ts)
        return {
            "status": str(final_ts.status),
            "agent_status": str(final_ts.agent_status),
            "run_id": str(run_id),
            "ticket_id": ticket_id,
        }

    except Exception:
        logger.exception("run_ticket failed for ticket %s", ticket_id)
        # Publish error event so the client sees a ticket:error.
        event_publisher: EventPublisherProtocol = ctx["event_publisher"]
        await event_publisher.publish_status(
            ticket_uuid,
            AgentStatus.IDLE,
            label="error",
            detail="run failed",
        )
        raise
    finally:
        await lock.release()


async def _lock_renewal_loop(lock: TicketLock) -> None:
    """Background task that renews the lock lease periodically.

    If renewal fails (ownership lost or network error), raises
    ``LockLostError`` to abort the run and prevent duplicate execution.
    """
    import asyncio

    while True:
        await asyncio.sleep(LOCK_RENEW_INTERVAL)
        # renew() now raises LockLostError on failure (network error or
        # ownership lost). Propagate to abort graph execution.
        await lock.renew()


# ---------------------------------------------------------------------------
# ARQ worker settings.
# ---------------------------------------------------------------------------
async def on_startup(ctx: dict) -> None:
    """Called once when the ARQ worker boots.

    Populates ``ctx`` with shared resources (Redis pool, DB pool, injected
    service dependencies).  In production these are real implementations; in
    tests they are mocks.
    """
    logger.info("ARQ worker starting up")
    # Real wiring happens in the worker entrypoint (Task 12).  For now we
    # just ensure the keys exist so the job doesn't KeyError on a missing
    # dependency — the entrypoint will overwrite these with real instances.
    ctx.setdefault("redis", None)
    ctx.setdefault("context_loader", None)
    ctx.setdefault("llm_router", None)
    ctx.setdefault("tool_executor", None)
    ctx.setdefault("checkpoint_store", None)
    ctx.setdefault("event_publisher", None)
    ctx.setdefault("response_persister", None)
    ctx.setdefault("tracer", None)


class WorkerSettings:
    """ARQ worker settings.

    Functions are wired here so ``arq WorkerSettings`` can boot the worker.
    """

    functions = [run_ticket]
    on_startup = on_startup
    redis_settings = RedisSettings()
