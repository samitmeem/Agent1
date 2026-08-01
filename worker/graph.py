"""LangGraph ticket-processing graph.

Implements the 9-node state machine from `AgenticProject.md` §1.7:

    load_context
      -> classify_request
      -> plan
      -> model
      -> tool_required?
           -> execute_tool -> model
           -> request_oauth -> waiting_for_user
      -> compose_response
      -> persist_final
      -> complete

Every graph invocation is isolated by ``ticket_id``.  Checkpoints are
persisted to PostgreSQL at three points:

1. After tool selection (before tool execution).
2. Before tool execution.
3. After final response generation.

External dependencies (LLM router, tool executor, persistence) are injected
through protocols so the graph can be tested with mocks.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, TypedDict, runtime_checkable
from uuid import UUID

from contracts.enums import AgentStatus, TicketStatus
from contracts.models import AgentDecision, TicketState, ToolPayload
from worker.prompt_defense import wrap_tool_output

logger = logging.getLogger("worker.graph")


# ---------------------------------------------------------------------------
# Graph state — the dict that flows between LangGraph nodes.
# ---------------------------------------------------------------------------
class GraphState(TypedDict, total=False):
    """Mutable state passed through the graph.

    ``ticket_state`` is the persisted ``TicketState``; the remaining keys are
    transient values produced by individual nodes.
    """

    ticket_state: TicketState
    conversation: list[dict[str, Any]]
    classification: str
    plan: list[str]
    decision: AgentDecision
    tool_payload: ToolPayload | None
    tool_result: dict[str, Any] | None
    response_text: str
    error: str | None
    # Trace correlation — propagated from the ARQ job so spans across
    # Gateway → Redis → Worker → LLM → Tools share one correlation_id.
    correlation_id: UUID


# ---------------------------------------------------------------------------
# Dependency protocols — injected so tests can mock external services.
# ---------------------------------------------------------------------------
@runtime_checkable
class LLMRouterProtocol(Protocol):
    """LLM router contract — implemented by ``llm/router.py`` (Task 7)."""

    async def decide(
        self,
        ticket_state: TicketState,
        conversation: list[dict[str, Any]],
        plan: list[str],
    ) -> AgentDecision: ...


@runtime_checkable
class ToolExecutorProtocol(Protocol):
    """Tool executor contract — implemented in Task 8 (OAuth connectors)."""

    async def execute(self, payload: ToolPayload) -> dict[str, Any]: ...


@runtime_checkable
class CheckpointStoreProtocol(Protocol):
    """PostgreSQL checkpoint persistence for LangGraph state."""

    async def save_checkpoint(
        self,
        ticket_id: Any,
        run_id: Any,
        node_name: str,
        data: dict[str, Any],
    ) -> None: ...


@runtime_checkable
class EventPublisherProtocol(Protocol):
    """Publishes ``agent:status_update`` events to Redis Pub/Sub."""

    async def publish_status(
        self,
        ticket_id: Any,
        status: AgentStatus,
        label: str | None = None,
        tool_name: str | None = None,
        detail: str | None = None,
    ) -> None: ...


@runtime_checkable
class ContextLoaderProtocol(Protocol):
    """Loads ticket history, OAuth metadata, and checkpoint from PostgreSQL."""

    async def load(
        self, ticket_id: Any, run_id: Any
    ) -> tuple[list[dict[str, Any]], TicketState]: ...


@runtime_checkable
class ResponsePersisterProtocol(Protocol):
    """Persists the final assistant message and updates ticket status."""

    async def persist_final(
        self,
        ticket_id: Any,
        run_id: Any,
        response_text: str,
        selected_model: str | None,
        fallback_reason: str | None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Node implementations.
# ---------------------------------------------------------------------------
class TicketGraph:
    """Holds the injected dependencies and the node methods.

    Each node method has signature ``async (state) -> state`` matching
    LangGraph's expected callable shape.
    """

    def __init__(
        self,
        *,
        context_loader: ContextLoaderProtocol,
        router: LLMRouterProtocol,
        tool_executor: ToolExecutorProtocol,
        checkpoint_store: CheckpointStoreProtocol,
        event_publisher: EventPublisherProtocol,
        response_persister: ResponsePersisterProtocol,
        tracer: Any | None = None,
    ) -> None:
        self._context_loader = context_loader
        self._router = router
        self._tool_executor = tool_executor
        self._checkpoint_store = checkpoint_store
        self._event_publisher = event_publisher
        self._response_persister = response_persister
        self._tracer = tracer

    def _span(self, name: str, state: GraphState) -> Any:
        """Create a tracing span for a graph node, carrying ticket_id and
        correlation_id attributes for cross-service trace correlation.

        Returns a context manager (NoopSpan-compatible) or ``None`` when
        no tracer is injected.
        """
        if self._tracer is None:
            from contextlib import nullcontext

            return nullcontext()
        ts = state.get("ticket_state")
        ticket_id = str(ts.ticket_id) if ts is not None else None
        correlation_id = state.get("correlation_id")
        return self._tracer.start_span(
            f"graph.{name}",
            ticket_id=ticket_id,
            correlation_id=str(correlation_id) if correlation_id else None,
        )

    # -- Node 1: load_context ----------------------------------------------
    async def load_context(self, state: GraphState) -> GraphState:
        """Load ticket history and persisted ``TicketState`` from PostgreSQL."""
        with self._span("load_context", state):
            ts = state["ticket_state"]
            conversation, ticket_state = await self._context_loader.load(
                ts.ticket_id, ts.run_id
            )
            state["conversation"] = conversation
            state["ticket_state"] = ticket_state
            return state

    # -- Node 2: classify_request ------------------------------------------
    async def classify_request(self, state: GraphState) -> GraphState:
        """Classify the user's request type (e.g. question, action, oauth)."""
        with self._span("classify_request", state):
            await self._event_publisher.publish_status(
                state["ticket_state"].ticket_id,
                AgentStatus.PLANNING,
                label="classifying",
                correlation_id=state.get("correlation_id"),
            )
        # Lightweight classification — the LLM router handles the real
        # reasoning in the ``model`` node.  Here we just tag the intent.
        conversation = state.get("conversation", [])
        last_user = next(
            (m for m in reversed(conversation) if m.get("role") == "user"),
            None,
        )
        text = (last_user or {}).get("text", "")
        if any(kw in text.lower() for kw in ("oauth", "authorize", "connect")):
            state["classification"] = "oauth"
        elif any(kw in text.lower() for kw in ("create", "send", "file", "issue")):
            state["classification"] = "action"
        else:
            state["classification"] = "question"
        return state

    # -- Node 3: plan -------------------------------------------------------
    async def plan(self, state: GraphState) -> GraphState:
        """Build a simple execution plan based on classification."""
        with self._span("plan", state):
            classification = state.get("classification", "question")
            if classification == "action":
                state["plan"] = ["understand_request", "select_tool", "execute", "respond"]
            elif classification == "oauth":
                state["plan"] = ["request_oauth", "wait_for_user"]
            else:
                state["plan"] = ["understand_request", "respond"]
            return state

    # -- Node 4: model ------------------------------------------------------
    async def model(self, state: GraphState) -> GraphState:
        """Call the LLM router to get an ``AgentDecision``."""
        with self._span("model", state):
            await self._event_publisher.publish_status(
                state["ticket_state"].ticket_id,
                AgentStatus.GENERATING,
                label="model_call",
                correlation_id=state.get("correlation_id"),
            )
            ts = state["ticket_state"]
            decision = await self._router.decide(
                ts, state.get("conversation", []), state.get("plan", [])
            )
            state["decision"] = decision

            # Checkpoint 1: after tool selection (before tool execution).
            if decision.action == "call_tool" and decision.tool is not None:
                state["tool_payload"] = decision.tool
                await self._checkpoint_store.save_checkpoint(
                    ts.ticket_id,
                    ts.run_id,
                    "after_tool_selection",
                    {"tool_name": decision.tool.tool_name, "action": decision.action},
                )
            return state

    # -- Conditional edge: tool_required? ----------------------------------
    async def tool_required(self, state: GraphState) -> str:
        """Route based on the LLM decision."""
        decision = state.get("decision")
        if decision is None:
            return "compose_response"
        if decision.action == "call_tool":
            return "execute_tool"
        if decision.action == "request_oauth":
            return "request_oauth"
        return "compose_response"

    # -- Node 5a: execute_tool ---------------------------------------------
    async def execute_tool(self, state: GraphState) -> GraphState:
        """Execute the selected tool with idempotency."""
        with self._span("execute_tool", state):
            ts = state["ticket_state"]
            payload = state.get("tool_payload")
            if payload is None:
                state["error"] = "execute_tool called without a tool payload"
                return state

            await self._event_publisher.publish_status(
                ts.ticket_id,
                AgentStatus.CALLING_TOOL,
                label="executing_tool",
                tool_name=payload.tool_name,
                correlation_id=state.get("correlation_id"),
            )

            # Checkpoint 2: before tool execution.
            await self._checkpoint_store.save_checkpoint(
                ts.ticket_id,
                ts.run_id,
                "before_tool_execution",
                {"tool_name": payload.tool_name, "idempotency_key": payload.idempotency_key},
            )

            try:
                result = await self._tool_executor.execute(payload)
                state["tool_result"] = result
            except Exception as exc:
                logger.exception("tool execution failed for %s", payload.tool_name)
                state["error"] = str(exc)
                state["tool_result"] = {"error": str(exc), "tool_name": payload.tool_name}
            return state

    # -- Node 5b: request_oauth --------------------------------------------
    async def request_oauth(self, state: GraphState) -> GraphState:
        """Set ticket to ``waiting_for_user`` for OAuth authorization."""
        with self._span("request_oauth", state):
            ts = state["ticket_state"]
            ts.status = TicketStatus.WAITING_FOR_USER
            ts.agent_status = AgentStatus.AWAITING_AUTHORIZATION
            decision = state.get("decision")
            if decision and decision.tool is not None:
                ts.pending_oauth_provider = "github" if "github" in decision.tool.tool_name else "gmail"
            await self._event_publisher.publish_status(
                ts.ticket_id,
                AgentStatus.AWAITING_AUTHORIZATION,
                label="oauth_required",
                detail=f"provider={ts.pending_oauth_provider}",
                correlation_id=state.get("correlation_id"),
            )
            state["ticket_state"] = ts
            return state

    # -- Node 5c: waiting_for_user (terminal for this run) -----------------
    async def waiting_for_user(self, state: GraphState) -> GraphState:
        """Terminal node — the run pauses until the user authorizes."""
        return state

    # -- Node 6: compose_response ------------------------------------------
    async def compose_response(self, state: GraphState) -> GraphState:
        """Compose the final response text from the decision and tool result."""
        with self._span("compose_response", state):
            ts = state["ticket_state"]
            decision = state.get("decision")

            if decision and decision.action == "respond" and decision.response_text:
                state["response_text"] = decision.response_text
            elif state.get("tool_result"):
                result = state["tool_result"]
                tool_name = result.get("tool_name", "a tool")
                if "error" in result:
                    # Wrap untrusted tool output to prevent indirect prompt
                    # injection from external APIs.
                    safe_output = wrap_tool_output(tool_name, str(result["error"]))
                    state["response_text"] = (
                        f"I attempted to use {tool_name} "
                        f"but encountered an error: {safe_output}"
                    )
                else:
                    # Wrap untrusted tool output to prevent indirect prompt
                    # injection from external APIs.
                    safe_output = wrap_tool_output(tool_name, str(result))
                    state["response_text"] = (
                        f"I've completed the requested action. Result: {safe_output}"
                    )
            else:
                state["response_text"] = "I'm here to help. Could you provide more details?"

            # Checkpoint 3: after final response generation.
            await self._checkpoint_store.save_checkpoint(
                ts.ticket_id,
                ts.run_id,
                "after_final_response",
                {"response_text": state["response_text"]},
            )
            return state

    # -- Node 7: persist_final ----------------------------------------------
    async def persist_final(self, state: GraphState) -> GraphState:
        """Persist the assistant message and update ticket status."""
        with self._span("persist_final", state):
            ts = state["ticket_state"]
            await self._response_persister.persist_final(
                ts.ticket_id,
                ts.run_id,
                state.get("response_text", ""),
                ts.selected_model,
                ts.fallback_reason,
            )
            return state

    # -- Node 8: complete ---------------------------------------------------
    async def complete(self, state: GraphState) -> GraphState:
        """Mark the ticket as completed and emit final status."""
        with self._span("complete", state):
            ts = state["ticket_state"]
            ts.agent_status = AgentStatus.COMPLETED
            if ts.status != TicketStatus.WAITING_FOR_USER:
                ts.status = TicketStatus.RESOLVED
            await self._event_publisher.publish_status(
                ts.ticket_id,
                AgentStatus.COMPLETED,
                label="done",
                correlation_id=state.get("correlation_id"),
            )
            state["ticket_state"] = ts
            return state


# ---------------------------------------------------------------------------
# Graph builder.
# ---------------------------------------------------------------------------
def build_graph(graph_nodes: TicketGraph) -> Any:
    """Build and compile the LangGraph ``StateGraph``.

    Returns a compiled graph that can be invoked with ``ainvoke(state)``.
    The graph has the 9 nodes in the required order with a conditional edge
    after ``model`` that routes to ``execute_tool``, ``request_oauth``, or
    ``compose_response``.
    """
    from langgraph.graph import END, StateGraph

    workflow = StateGraph(GraphState)

    # Register all 9 nodes.
    workflow.add_node("load_context", graph_nodes.load_context)
    workflow.add_node("classify_request", graph_nodes.classify_request)
    workflow.add_node("plan", graph_nodes.plan)
    workflow.add_node("model", graph_nodes.model)
    workflow.add_node("execute_tool", graph_nodes.execute_tool)
    workflow.add_node("request_oauth", graph_nodes.request_oauth)
    workflow.add_node("waiting_for_user", graph_nodes.waiting_for_user)
    workflow.add_node("compose_response", graph_nodes.compose_response)
    workflow.add_node("persist_final", graph_nodes.persist_final)
    workflow.add_node("complete", graph_nodes.complete)

    # Linear edges: load_context -> classify -> plan -> model
    workflow.set_entry_point("load_context")
    workflow.add_edge("load_context", "classify_request")
    workflow.add_edge("classify_request", "plan")
    workflow.add_edge("plan", "model")

    # Conditional edge after model: tool_required?
    workflow.add_conditional_edges(
        "model",
        graph_nodes.tool_required,
        {
            "execute_tool": "execute_tool",
            "request_oauth": "request_oauth",
            "compose_response": "compose_response",
        },
    )

    # execute_tool loops back to model (re-evaluate after tool result).
    workflow.add_edge("execute_tool", "model")

    # request_oauth -> waiting_for_user (terminal for this run).
    workflow.add_edge("request_oauth", "waiting_for_user")
    workflow.add_edge("waiting_for_user", END)

    # compose_response -> persist_final -> complete -> END
    workflow.add_edge("compose_response", "persist_final")
    workflow.add_edge("persist_final", "complete")
    workflow.add_edge("complete", END)

    return workflow.compile()
