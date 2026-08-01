"""Tests for the worker LangGraph and ARQ job.

Covers:
- Graph node ordering (all 9 nodes present).
- Three checkpoint persistence points.
- Tool execution path (model -> execute_tool -> model -> compose_response).
- OAuth path (model -> request_oauth -> waiting_for_user).
- Direct response path (model -> compose_response -> persist_final -> complete).
- ``run_ticket`` job: lock acquisition, safe exit when already locked,
  lock release on completion.
- Lock renewal and ownership loss.
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from contracts.enums import AgentStatus, TicketStatus
from contracts.models import AgentDecision, TicketState, ToolPayload
from worker.graph import (
    GraphState,
    TicketGraph,
    build_graph,
)
from worker.jobs import TicketLock, run_ticket


# ---------------------------------------------------------------------------
# Mock implementations of the dependency protocols.
# ---------------------------------------------------------------------------
class MockContextLoader:
    """Returns a canned conversation and the ticket state."""

    def __init__(self, conversation: list[dict[str, Any]] | None = None):
        self.conversation = conversation or [
            {"role": "user", "text": "Create a GitHub issue for the login bug"},
        ]
        self.load_count = 0

    async def load(self, ticket_id: Any, run_id: Any) -> tuple[list[dict[str, Any]], TicketState]:
        from datetime import datetime, timezone

        self.load_count += 1
        ts = TicketState(
            ticket_id=ticket_id,
            user_id=uuid4(),
            run_id=run_id,
            updated_at=datetime.now(timezone.utc),
        )
        return self.conversation, ts


class MockRouter:
    """Returns a canned ``AgentDecision`` based on ``action``.

    For ``call_tool`` mode, the first call returns the tool decision and
    subsequent calls return ``respond`` so the ``execute_tool -> model`` loop
    terminates (simulating the LLM deciding to respond after seeing the tool
    result).  The response text references the tool result or error so tests
    can assert on it.
    """

    def __init__(
        self,
        action: str = "respond",
        response_text: str = "Hello!",
        tool: ToolPayload | None = None,
        tool_result: dict[str, Any] | None = None,
        tool_error: str | None = None,
    ):
        self.action = action
        self.response_text = response_text
        self.tool = tool
        self.tool_result = tool_result
        self.tool_error = tool_error
        self.call_count = 0

    async def decide(self, ticket_state, conversation, plan) -> AgentDecision:
        self.call_count += 1
        # After the first call_tool decision, switch to respond so the
        # execute_tool -> model loop terminates.
        if self.action == "call_tool" and self.call_count > 1:
            if self.tool_error:
                return AgentDecision(
                    action="respond",
                    response_text=f"I encountered an error: {self.tool_error}",
                )
            return AgentDecision(
                action="respond",
                response_text=f"Tool executed successfully. Result: {self.tool_result}",
            )
        return AgentDecision(
            action=self.action,
            response_text=self.response_text if self.action == "respond" else None,
            tool=self.tool if self.action in ("call_tool", "request_oauth") else None,
        )


class MockToolExecutor:
    """Returns a canned tool result."""

    def __init__(self, result: dict[str, Any] | None = None, raise_exc: Exception | None = None):
        self.result = result or {"issue_number": 42, "url": "https://github.com/test/repo/issues/42"}
        self.raise_exc = raise_exc
        self.call_count = 0

    async def execute(self, payload: ToolPayload) -> dict[str, Any]:
        self.call_count += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.result


class MockCheckpointStore:
    """Records all checkpoint saves."""

    def __init__(self):
        self.checkpoints: list[dict[str, Any]] = []

    async def save_checkpoint(self, ticket_id, run_id, node_name, data) -> None:
        self.checkpoints.append({
            "ticket_id": ticket_id,
            "run_id": run_id,
            "node_name": node_name,
            "data": data,
        })


class MockEventPublisher:
    """Records all published status events."""

    def __init__(self):
        self.events: list[dict[str, Any]] = []

    async def publish_status(self, ticket_id, status, label=None, tool_name=None, detail=None, correlation_id=None) -> None:
        self.events.append({
            "ticket_id": ticket_id,
            "status": status,
            "label": label,
            "tool_name": tool_name,
            "detail": detail,
            "correlation_id": correlation_id,
        })


class MockResponsePersister:
    """Records the final persisted response."""

    def __init__(self):
        self.persisted: list[dict[str, Any]] = []

    async def persist_final(self, ticket_id, run_id, response_text, selected_model, fallback_reason) -> None:
        self.persisted.append({
            "ticket_id": ticket_id,
            "run_id": run_id,
            "response_text": response_text,
            "selected_model": selected_model,
            "fallback_reason": fallback_reason,
        })


class MockRedis:
    """Minimal Redis mock for lock tests — supports SET NX, GET, DEL, EXPIRE, eval."""

    def __init__(self):
        self._store: dict[str, bytes] = {}
        self._ttls: dict[str, int] = {}

    async def set(self, key, value, nx=False, ex=None, **kwargs):
        if nx and key in self._store:
            return None
        self._store[key] = value
        if ex:
            self._ttls[key] = ex
        return True

    async def get(self, key):
        return self._store.get(key)

    async def delete(self, key):
        return self._store.pop(key, None) is not None

    async def expire(self, key, seconds):
        if key in self._store:
            self._ttls[key] = seconds
            return True
        return False

    async def eval(self, script, numkeys, *args):
        key = args[0]
        token = args[1]
        if script.strip().startswith("if redis.call('GET'"):
            stored = self._store.get(key)
            if stored == token:
                if "EXPIRE" in script:
                    self._ttls[key] = int(args[2])
                    return True
                elif "DEL" in script:
                    self._store.pop(key, None)
                    return True
            return False
        return None


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------
def _make_tool_payload() -> ToolPayload:
    return ToolPayload(
        execution_id=uuid4(),
        ticket_id=uuid4(),
        user_id=uuid4(),
        tool_name="github.create_issue",
        idempotency_key="idem-key-0123456789abcdef",
        arguments={"repository": "test/repo", "title": "Bug", "body": "Login fails"},
    )


def _make_ticket_state() -> TicketState:
    from datetime import datetime, timezone

    return TicketState(
        ticket_id=uuid4(),
        user_id=uuid4(),
        run_id=uuid4(),
        updated_at=datetime.now(timezone.utc),
    )


def _make_graph_nodes(
    *,
    router_action: str = "respond",
    router_text: str = "I can help with that.",
    tool: ToolPayload | None = None,
    tool_result: dict | None = None,
    tool_exc: Exception | None = None,
    conversation: list[dict] | None = None,
) -> tuple[TicketGraph, MockCheckpointStore, MockEventPublisher, MockResponsePersister, MockRouter, MockToolExecutor]:
    checkpoint_store = MockCheckpointStore()
    event_publisher = MockEventPublisher()
    response_persister = MockResponsePersister()
    tool_error = str(tool_exc) if tool_exc else None
    router = MockRouter(
        action=router_action,
        response_text=router_text,
        tool=tool,
        tool_result=tool_result,
        tool_error=tool_error,
    )
    tool_executor = MockToolExecutor(result=tool_result, raise_exc=tool_exc)
    context_loader = MockContextLoader(conversation=conversation)

    graph_nodes = TicketGraph(
        context_loader=context_loader,
        router=router,
        tool_executor=tool_executor,
        checkpoint_store=checkpoint_store,
        event_publisher=event_publisher,
        response_persister=response_persister,
    )
    return graph_nodes, checkpoint_store, event_publisher, response_persister, router, tool_executor


# ---------------------------------------------------------------------------
# Graph structure tests.
# ---------------------------------------------------------------------------
class TestGraphStructure:
    """Verify the graph has all 9 nodes and correct edges."""

    def test_build_graph_returns_compiled_graph(self):
        graph_nodes, *_ = _make_graph_nodes()
        graph = build_graph(graph_nodes)
        assert graph is not None

    def test_graph_has_all_nine_nodes(self):
        graph_nodes, *_ = _make_graph_nodes()
        graph = build_graph(graph_nodes)
        node_names = set(graph.nodes.keys())
        expected = {
            "load_context",
            "classify_request",
            "plan",
            "model",
            "execute_tool",
            "request_oauth",
            "waiting_for_user",
            "compose_response",
            "persist_final",
            "complete",
        }
        assert expected.issubset(node_names), f"missing nodes: {expected - node_names}"


# ---------------------------------------------------------------------------
# Graph execution path tests.
# ---------------------------------------------------------------------------
class TestDirectResponsePath:
    """model -> compose_response -> persist_final -> complete."""

    @pytest.mark.asyncio
    async def test_direct_response_flow(self):
        graph_nodes, checkpoints, events, persister, router, _ = _make_graph_nodes(
            router_action="respond",
            router_text="Here is your answer.",
        )
        graph = build_graph(graph_nodes)
        ts = _make_ticket_state()
        result = await graph.ainvoke({"ticket_state": ts})

        assert result["response_text"] == "Here is your answer."
        assert router.call_count == 1
        # Response persisted.
        assert len(persister.persisted) == 1
        assert persister.persisted[0]["response_text"] == "Here is your answer."
        # Ticket completed.
        final_ts: TicketState = result["ticket_state"]
        assert final_ts.agent_status == AgentStatus.COMPLETED
        assert final_ts.status == TicketStatus.RESOLVED
        # Checkpoint 3: after final response.
        cp_names = [c["node_name"] for c in checkpoints.checkpoints]
        assert "after_final_response" in cp_names


class TestToolExecutionPath:
    """model -> execute_tool -> model -> compose_response."""

    @pytest.mark.asyncio
    async def test_tool_execution_flow(self):
        tool = _make_tool_payload()
        graph_nodes, checkpoints, events, persister, router, tool_executor = _make_graph_nodes(
            router_action="call_tool",
            tool=tool,
            tool_result={"issue_number": 99, "url": "https://github.com/test/repo/issues/99"},
        )
        graph = build_graph(graph_nodes)
        ts = _make_ticket_state()
        result = await graph.ainvoke({"ticket_state": ts})

        # Tool was executed.
        assert tool_executor.call_count == 1
        # Router called twice: once before tool, once after (loop back to model).
        assert router.call_count >= 2
        # Checkpoint 1: after tool selection.
        cp_names = [c["node_name"] for c in checkpoints.checkpoints]
        assert "after_tool_selection" in cp_names
        # Checkpoint 2: before tool execution.
        assert "before_tool_execution" in cp_names
        # Checkpoint 3: after final response.
        assert "after_final_response" in cp_names
        # Response composed from tool result.
        assert "issue_number" in result["response_text"]
        # Response persisted.
        assert len(persister.persisted) == 1

    @pytest.mark.asyncio
    async def test_tool_execution_error_handled(self):
        tool = _make_tool_payload()
        graph_nodes, checkpoints, events, persister, router, tool_executor = _make_graph_nodes(
            router_action="call_tool",
            tool=tool,
            tool_exc=RuntimeError("GitHub API returned 403"),
        )
        graph = build_graph(graph_nodes)
        ts = _make_ticket_state()
        result = await graph.ainvoke({"ticket_state": ts})

        # Error captured in state.
        assert result.get("error") is not None
        assert "403" in result["error"]
        # Response composed with error message (router returns error text on
        # the second call after seeing the tool failure).
        assert "403" in result["response_text"]


class TestOAuthPath:
    """model -> request_oauth -> waiting_for_user."""

    @pytest.mark.asyncio
    async def test_oauth_flow(self):
        tool = _make_tool_payload()
        # Use a github tool so pending_oauth_provider = "github"
        graph_nodes, checkpoints, events, persister, router, _ = _make_graph_nodes(
            router_action="request_oauth",
            tool=tool,
        )
        graph = build_graph(graph_nodes)
        ts = _make_ticket_state()
        result = await graph.ainvoke({"ticket_state": ts})

        final_ts: TicketState = result["ticket_state"]
        assert final_ts.status == TicketStatus.WAITING_FOR_USER
        assert final_ts.agent_status == AgentStatus.AWAITING_AUTHORIZATION
        assert final_ts.pending_oauth_provider == "github"
        # No response persisted (waiting for user).
        assert len(persister.persisted) == 0


# ---------------------------------------------------------------------------
# Checkpoint persistence tests.
# ---------------------------------------------------------------------------
class TestCheckpoints:
    """Verify checkpoints are persisted at the 3 required points."""

    @pytest.mark.asyncio
    async def test_three_checkpoints_on_tool_path(self):
        tool = _make_tool_payload()
        graph_nodes, checkpoints, *_ = _make_graph_nodes(
            router_action="call_tool",
            tool=tool,
        )
        graph = build_graph(graph_nodes)
        ts = _make_ticket_state()
        await graph.ainvoke({"ticket_state": ts})

        cp_names = [c["node_name"] for c in checkpoints.checkpoints]
        assert "after_tool_selection" in cp_names
        assert "before_tool_execution" in cp_names
        assert "after_final_response" in cp_names

    @pytest.mark.asyncio
    async def test_checkpoint_data_contains_tool_name(self):
        tool = _make_tool_payload()
        graph_nodes, checkpoints, *_ = _make_graph_nodes(
            router_action="call_tool",
            tool=tool,
        )
        graph = build_graph(graph_nodes)
        ts = _make_ticket_state()
        await graph.ainvoke({"ticket_state": ts})

        selection_cp = next(
            c for c in checkpoints.checkpoints if c["node_name"] == "after_tool_selection"
        )
        assert selection_cp["data"]["tool_name"] == "github.create_issue"


# ---------------------------------------------------------------------------
# TicketLock tests.
# ---------------------------------------------------------------------------
class TestTicketLock:
    @pytest.mark.asyncio
    async def test_acquire_lock_succeeds(self):
        redis = MockRedis()
        ticket_id = uuid4()
        lock = TicketLock(redis, ticket_id)
        acquired = await lock.acquire()
        assert acquired is True

    @pytest.mark.asyncio
    async def test_second_acquire_fails(self):
        redis = MockRedis()
        ticket_id = uuid4()
        lock1 = TicketLock(redis, ticket_id)
        lock2 = TicketLock(redis, ticket_id)
        assert await lock1.acquire() is True
        assert await lock2.acquire() is False

    @pytest.mark.asyncio
    async def test_renew_extends_lease(self):
        redis = MockRedis()
        ticket_id = uuid4()
        lock = TicketLock(redis, ticket_id)
        await lock.acquire()
        renewed = await lock.renew()
        assert renewed is True

    @pytest.mark.asyncio
    async def test_renew_fails_if_lost(self):
        redis = MockRedis()
        ticket_id = uuid4()
        lock = TicketLock(redis, ticket_id)
        await lock.acquire()
        # Simulate another worker overwriting the lock.
        redis._store[lock.key] = b"other-token"
        # renew() now raises LockLostError (fail-fast) instead of returning False.
        from worker.jobs import LockLostError

        with pytest.raises(LockLostError):
            await lock.renew()

    @pytest.mark.asyncio
    async def test_release_clears_lock(self):
        redis = MockRedis()
        ticket_id = uuid4()
        lock = TicketLock(redis, ticket_id)
        await lock.acquire()
        await lock.release()
        assert lock.key not in redis._store

    @pytest.mark.asyncio
    async def test_release_safe_if_not_held(self):
        redis = MockRedis()
        ticket_id = uuid4()
        lock = TicketLock(redis, ticket_id)
        # Release without acquire — should not raise.
        await lock.release()


# ---------------------------------------------------------------------------
# run_ticket job tests.
# ---------------------------------------------------------------------------
class TestRunTicketJob:
    """Tests for the ARQ job with lock lifecycle."""

    def _make_ctx(self, redis=None, router_action="respond") -> dict:
        if redis is None:
            redis = MockRedis()
        checkpoint_store = MockCheckpointStore()
        event_publisher = MockEventPublisher()
        response_persister = MockResponsePersister()
        router = MockRouter(action=router_action, response_text="Done.")
        tool_executor = MockToolExecutor()
        context_loader = MockContextLoader()
        return {
            "redis": redis,
            "context_loader": context_loader,
            "llm_router": router,
            "tool_executor": tool_executor,
            "checkpoint_store": checkpoint_store,
            "event_publisher": event_publisher,
            "response_persister": response_persister,
            "user_id": uuid4(),
        }

    @pytest.mark.asyncio
    async def test_run_ticket_completes(self):
        ctx = self._make_ctx(router_action="respond")
        ticket_id = str(uuid4())
        result = await run_ticket(ctx, ticket_id, str(uuid4()))
        assert result["status"] == str(TicketStatus.RESOLVED)
        assert result["ticket_id"] == ticket_id
        assert "run_id" in result

    @pytest.mark.asyncio
    async def test_run_ticket_exits_if_already_running(self):
        redis = MockRedis()
        ticket_id = uuid4()
        # Pre-set the lock key to simulate another worker holding it.
        redis._store[f"lock:ticket:{ticket_id}"] = b"other-token"

        ctx = self._make_ctx(redis=redis)
        result = await run_ticket(ctx, str(ticket_id), str(uuid4()))
        assert result["status"] == "already_running"

    @pytest.mark.asyncio
    async def test_run_ticket_releases_lock_on_completion(self):
        redis = MockRedis()
        ctx = self._make_ctx(redis=redis, router_action="respond")
        ticket_id = str(uuid4())
        await run_ticket(ctx, ticket_id, str(uuid4()))
        # Lock should be released after completion.
        assert f"lock:ticket:{ticket_id}" not in redis._store

    @pytest.mark.asyncio
    async def test_run_ticket_releases_lock_on_error(self):
        redis = MockRedis()
        ctx = self._make_ctx(redis=redis)
        # Make the router raise to trigger the job's error path.
        ctx["llm_router"] = _RaisingRouter()
        ticket_id = str(uuid4())
        with pytest.raises(RuntimeError):
            await run_ticket(ctx, ticket_id, str(uuid4()))
        # Lock should still be released even on error.
        assert f"lock:ticket:{ticket_id}" not in redis._store


class _RaisingRouter:
    """Router that always raises, to test error cleanup in run_ticket."""

    async def decide(self, *args, **kwargs) -> AgentDecision:
        raise RuntimeError("LLM router unavailable")
