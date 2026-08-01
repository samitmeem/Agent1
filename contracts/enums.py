"""Enums shared across Gateway, Worker, and LLM router.

Mirrors the PostgreSQL enums created in `database/migrations/versions/0001_initial.py`
and the Pydantic models in `AgenticProject.md` §2.
"""
from __future__ import annotations

from enum import StrEnum


class TicketStatus(StrEnum):
    """Lifecycle status of a ticket, persisted in `tickets.status`."""

    OPEN = "open"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    RESOLVED = "resolved"
    FAILED = "failed"


class AgentStatus(StrEnum):
    """Worker-side agent state, surfaced via `agent:status_update` events."""

    IDLE = "idle"
    PLANNING = "planning"
    GENERATING = "generating"
    CALLING_TOOL = "calling_tool"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    SYNTHESIZING_VOICE = "synthesizing_voice"
    COMPLETED = "completed"
