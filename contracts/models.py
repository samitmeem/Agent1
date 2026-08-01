"""Core Pydantic models for ticket state, tool payloads, and LLM decisions.

Source of truth: `AgenticProject.md` §2 (Pydantic models) and §2 (Validated
router contract). These models are shared by the Gateway, Worker, and LLM
router so that every component validates against the same schema.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from contracts.enums import AgentStatus, TicketStatus


class TicketState(BaseModel):
    """Per-ticket reasoning and execution state, checkpointed by LangGraph."""

    ticket_id: UUID
    user_id: UUID
    run_id: UUID
    status: TicketStatus = TicketStatus.OPEN
    agent_status: AgentStatus = AgentStatus.IDLE
    conversation_message_ids: list[UUID] = Field(default_factory=list)
    current_tool_execution_id: UUID | None = None
    pending_oauth_provider: Literal["github", "gmail"] | None = None
    retry_count: int = Field(default=0, ge=0, le=3)
    selected_model: str | None = None
    fallback_reason: str | None = None
    last_event_sequence: int = Field(default=0, ge=0)
    updated_at: datetime


class ToolPayload(BaseModel):
    """Validated, idempotent tool-call envelope.

    `execution_id` + `idempotency_key` are reused across fallback retries so a
    retried tool call never produces duplicate external side effects.
    """

    execution_id: UUID
    ticket_id: UUID
    user_id: UUID
    # The 4 built-in tools are strictly validated via Literal. Hermes tools
    # (vendored via the opt-in adapter, gated by ENABLE_HERMES_TOOLS=1) are
    # namespaced ``hermes.<name>`` and accepted by the validator below.
    tool_name: Literal[
        "github.create_issue",
        "github.comment_issue",
        "gmail.send_email",
        "gmail.search_messages",
    ] | str
    idempotency_key: str = Field(min_length=16, max_length=128)
    arguments: dict[str, Any]
    oauth_connection_id: UUID | None = None
    dry_run: bool = False

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, value: str) -> str:
        """Accept the 4 built-in tools or any ``hermes.*`` namespaced tool."""
        built_in = {
            "github.create_issue",
            "github.comment_issue",
            "gmail.send_email",
            "gmail.search_messages",
        }
        if value in built_in:
            return value
        if value.startswith("hermes."):
            return value
        raise ValueError(
            f"tool_name must be one of {sorted(built_in)} or a 'hermes.*' namespaced tool"
        )

    @field_validator("arguments")
    @classmethod
    def require_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject empty arguments — a tool call must carry intent."""
        if not value:
            raise ValueError("Tool arguments cannot be empty")
        return value


class GitHubCreateIssueArgs(BaseModel):
    """Arguments for `github.create_issue`, validated before execution."""

    repository: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1)
    labels: list[str] = Field(default_factory=list)


class GatewayEvent(BaseModel):
    """Minimal envelope for a durable event persisted in `events`."""

    type: str
    ticket_id: UUID
    correlation_id: UUID
    payload: dict[str, Any]


class AgentDecision(BaseModel):
    """Validated LLM decision returned by `LLMRouter.decide`.

    Exactly one of `response_text` / `tool` is populated depending on
    `action`. The router validates this before any tool is executed.

    ``selected_model`` and ``fallback_reason`` are populated by the router
    (not the LLM) to record which model served the request and why a
    fallback was triggered.
    """

    action: Literal["respond", "call_tool", "request_oauth"]
    response_text: str | None = None
    tool: ToolPayload | None = None
    selected_model: str | None = None
    fallback_reason: str | None = None
