"""Base classes and protocols for tool execution.

Defines the ``Tool`` abstract base, the ``ToolExecutor`` dependency protocols,
and shared dataclasses.  All external dependencies (DB, Redis, HTTP) are
injected through protocols so tests can mock them.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import httpx
from redis.asyncio import Redis

from contracts.models import ToolPayload

logger = logging.getLogger("worker.tools")

# ---------------------------------------------------------------------------
# Allow-list — enforced on every tool call.
# ---------------------------------------------------------------------------
ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "github.create_issue",
        "github.comment_issue",
        "gmail.send_email",
        "gmail.search_messages",
    }
)

# Tools that require explicit user confirmation unless an automation policy
# flag is set.
SENSITIVE_TOOLS: frozenset[str] = frozenset(
    {
        "gmail.send_email",
    }
)

# Default HTTP timeout for tool calls (seconds).
TOOL_HTTP_TIMEOUT = 30.0

# Rate limit: max calls per window per user+tool.
RATE_LIMIT_MAX_CALLS = 10
RATE_LIMIT_WINDOW_SECONDS = 60


# ---------------------------------------------------------------------------
# Dataclasses.
# ---------------------------------------------------------------------------
@dataclass
class ToolResult:
    """Result returned by a tool execution."""

    success: bool
    tool_name: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dict for JSON serialization."""
        return {
            "success": self.success,
            "tool_name": self.tool_name,
            "data": self.data,
            "error": self.error,
        }


@dataclass
class OAuthCredential:
    """Decrypted OAuth credential retrieved from PostgreSQL.

    Tokens are never stored in LangGraph state — only ``oauth_connection_id``
    is passed through the graph.
    """

    connection_id: UUID
    provider: str  # "github" | "gmail"
    access_token: str | None
    refresh_token_encrypted: bytes | None
    access_token_expires_at: datetime | None
    scopes: list[str] | None = None
    provider_user_id: str | None = None
    provider_user_email: str | None = None


@dataclass
class ToolExecutionRecord:
    """Represents a row in the ``tool_executions`` table."""

    execution_id: UUID
    ticket_id: UUID
    user_id: UUID
    tool_name: str
    idempotency_key: str
    arguments: dict[str, Any]
    oauth_connection_id: UUID | None
    dry_run: bool
    status: str  # pending | running | succeeded | failed
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Dependency protocols.
# ---------------------------------------------------------------------------
@runtime_checkable
class ToolExecutionStoreProtocol(Protocol):
    """PostgreSQL persistence for ``tool_executions`` rows.

    The unique constraint on ``idempotency_key`` means a retry that reuses
    the same key will hit a conflict — the store should return the existing
    row's result instead of executing again.
    """

    async def insert_pending(self, record: ToolExecutionRecord) -> None:
        """Insert a ``pending`` row before invocation.

        Raises if ``idempotency_key`` already exists (duplicate).
        """
        ...

    async def mark_running(self, execution_id: UUID) -> None:
        """Set status to ``running`` and record ``started_at``."""
        ...

    async def mark_completed(
        self,
        execution_id: UUID,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        """Set status to ``succeeded``/``failed``, record ``completed_at``."""
        ...

    async def get_by_idempotency_key(self, idempotency_key: str) -> ToolExecutionRecord | None:
        """Look up an existing execution by idempotency key.

        Returns ``None`` if not found.  Used to short-circuit retries.
        """
        ...


@runtime_checkable
class OAuthCredentialStoreProtocol(Protocol):
    """Retrieves and refreshes OAuth credentials from PostgreSQL."""

    async def get_credential(
        self,
        user_id: UUID,
        provider: str,
    ) -> OAuthCredential | None:
        """Retrieve the encrypted OAuth credential for a user+provider."""
        ...

    async def refresh_token(
        self,
        connection_id: UUID,
        refresh_token_encrypted: bytes,
    ) -> OAuthCredential:
        """Refresh the access token using the encrypted refresh token.

        Updates the ``access_token`` and ``access_token_expires_at`` columns
        in PostgreSQL.  Called under a per-connection Redis lock.
        """
        ...

    async def update_access_token(
        self,
        connection_id: UUID,
        access_token: str,
        expires_at: datetime,
    ) -> None:
        """Persist a refreshed access token."""
        ...


@runtime_checkable
class UserConfirmationProtocol(Protocol):
    """Checks whether a user has confirmed a sensitive tool action."""

    async def is_confirmed(
        self,
        user_id: UUID,
        tool_name: str,
        idempotency_key: str,
    ) -> bool:
        """Return ``True`` if the user has confirmed this action.

        Also returns ``True`` if an approved automation policy exists for
        this user+tool combination.
        """
        ...


@runtime_checkable
class RateLimiterProtocol(Protocol):
    """Per-user, per-tool rate limiter."""

    async def allow(self, user_id: UUID, tool_name: str) -> bool:
        """Return ``True`` if the call is within the rate limit."""
        ...


# ---------------------------------------------------------------------------
# Redis-based rate limiter.
# ---------------------------------------------------------------------------
class RedisRateLimiter:
    """Token-bucket rate limiter using Redis INCR with TTL.

    Allows ``RATE_LIMIT_MAX_CALLS`` per ``RATE_LIMIT_WINDOW_SECONDS`` per
    user+tool combination.
    """

    def __init__(
        self,
        redis: Redis,
        max_calls: int = RATE_LIMIT_MAX_CALLS,
        window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self._redis = redis
        self._max_calls = max_calls
        self._window = window_seconds

    async def allow(self, user_id: UUID, tool_name: str) -> bool:
        key = f"ratelimit:{user_id}:{tool_name}"
        # INCR is atomic; first call sets it to 1.
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, self._window)
        return count <= self._max_calls


# ---------------------------------------------------------------------------
# Abstract Tool base class.
# ---------------------------------------------------------------------------
class Tool(abc.ABC):
    """Abstract base for all tools.

    Subclasses implement ``_invoke`` which makes the actual HTTP call.
    The ``execute`` method handles allow-list enforcement, rate limiting,
    and result wrapping.
    """

    name: str = ""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = TOOL_HTTP_TIMEOUT,
    ) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._timeout = timeout

    async def execute(
        self,
        payload: ToolPayload,
        credential: OAuthCredential | None,
    ) -> ToolResult:
        """Execute the tool with allow-list enforcement."""
        if self.name not in ALLOWED_TOOLS:
            return ToolResult(
                success=False,
                tool_name=self.name,
                error=f"tool '{self.name}' is not in the allow-list",
            )
        try:
            return await self._invoke(payload, credential)
        except httpx.TimeoutException as exc:
            logger.warning("tool %s timed out: %s", self.name, exc)
            return ToolResult(
                success=False,
                tool_name=self.name,
                error=f"timeout: {exc}",
            )
        except httpx.HTTPError as exc:
            logger.warning("tool %s HTTP error: %s", self.name, exc)
            return ToolResult(
                success=False,
                tool_name=self.name,
                error=f"http error: {exc}",
            )
        except Exception as exc:
            logger.exception("tool %s failed: %s", self.name, exc)
            return ToolResult(
                success=False,
                tool_name=self.name,
                error=f"unexpected error: {exc}",
            )

    @abc.abstractmethod
    async def _invoke(
        self,
        payload: ToolPayload,
        credential: OAuthCredential | None,
    ) -> ToolResult:
        """Make the actual API call. Subclasses implement this."""
        ...

    @staticmethod
    def _require_credential(
        credential: OAuthCredential | None,
        tool_name: str,
    ) -> OAuthCredential:
        """Raise if no credential is provided."""
        if credential is None or not credential.access_token:
            raise ValueError(
                f"tool '{tool_name}' requires an OAuth credential with an access token"
            )
        return credential
