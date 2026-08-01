"""ToolExecutor — orchestrates tool execution with idempotency and OAuth.

Satisfies ``worker.graph.ToolExecutorProtocol``.  Responsibilities:

1. **Idempotency**: check ``tool_executions`` by ``idempotency_key``; if a
   completed row exists, return its result without re-executing.
2. **Allow-list enforcement**: reject tools not in ``ALLOWED_TOOLS``.
3. **User confirmation**: for sensitive tools (``gmail.send_email``), check
   ``UserConfirmationProtocol`` before executing.
4. **Rate limiting**: check ``RateLimiterProtocol`` before executing.
5. **OAuth credential retrieval**: fetch encrypted credentials from
   PostgreSQL, refresh tokens under a per-connection Redis lock if expired.
6. **tool_executions lifecycle**: insert ``pending`` row before invocation,
   mark ``running`` when the HTTP call starts, mark ``succeeded``/``failed``
   after it completes.
7. **Dry-run**: if ``payload.dry_run`` is set, the tool returns a preview
   without making an external API call.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
from redis.asyncio import Redis

from contracts.models import ToolPayload
from worker.tools.base import (
    ALLOWED_TOOLS,
    OAuthCredential,
    OAuthCredentialStoreProtocol,
    RateLimiterProtocol,
    SENSITIVE_TOOLS,
    Tool,
    ToolExecutionRecord,
    ToolExecutionStoreProtocol,
    ToolResult,
    UserConfirmationProtocol,
)
from worker.tools.github import GitHubCommentIssue, GitHubCreateIssue
from worker.tools.gmail import GmailSearchMessages, GmailSendEmail

logger = logging.getLogger("worker.tools.executor")

# OAuth token refresh threshold — refresh if token expires within this many
# seconds.
TOKEN_REFRESH_THRESHOLD_SECONDS = 60

# Redis lock lease for OAuth token refresh.
OAUTH_LOCK_LEASE = 30
OAUTH_LOCK_PREFIX = "lock:oauth:"


# ---------------------------------------------------------------------------
# OAuth connection lock.
# ---------------------------------------------------------------------------
class OAuthConnectionLock:
    """Per-connection Redis lock for OAuth token refresh.

    Prevents two concurrent tool executions from refreshing the same token
    simultaneously, which would invalidate one of the refresh attempts.
    """

    def __init__(self, redis: Redis, connection_id: UUID, lease: int = OAUTH_LOCK_LEASE):
        self._redis = redis
        self._connection_id = connection_id
        self._lease = lease
        self._token: bytes | None = None

    @property
    def key(self) -> str:
        return f"{OAUTH_LOCK_PREFIX}{self._connection_id}"

    async def acquire(self) -> bool:
        import os

        self._token = os.urandom(16)
        result = await self._redis.set(self.key, self._token, nx=True, ex=self._lease)
        return bool(result)

    async def release(self) -> None:
        if self._token is None:
            return
        # Only delete if we still own it.
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        else
            return 0
        end
        """
        try:
            await self._redis.eval(script, 1, self.key, self._token)
        except Exception:
            logger.warning("failed to release OAuth lock %s", self.key)
        finally:
            self._token = None


# ---------------------------------------------------------------------------
# ToolExecutor.
# ---------------------------------------------------------------------------
class ToolExecutor:
    """Orchestrates tool execution with idempotency, OAuth, and safety checks.

    Satisfies ``worker.graph.ToolExecutorProtocol``.
    """

    def __init__(
        self,
        *,
        execution_store: ToolExecutionStoreProtocol,
        oauth_store: OAuthCredentialStoreProtocol,
        confirmation_checker: UserConfirmationProtocol,
        rate_limiter: RateLimiterProtocol,
        redis: Redis,
        http_client: httpx.AsyncClient | None = None,
        tracer: Any | None = None,
        slo_tracker: Any | None = None,
        extra_tools: list[Tool] | None = None,
    ) -> None:
        self._execution_store = execution_store
        self._oauth_store = oauth_store
        self._confirmation_checker = confirmation_checker
        self._rate_limiter = rate_limiter
        self._redis = redis
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._tracer = tracer
        self._slo = slo_tracker

        # Build the tool registry.
        self._tools: dict[str, Tool] = {
            "github.create_issue": GitHubCreateIssue(http_client=self._http),
            "github.comment_issue": GitHubCommentIssue(http_client=self._http),
            "gmail.send_email": GmailSendEmail(http_client=self._http),
            "gmail.search_messages": GmailSearchMessages(http_client=self._http),
        }

        # Merge in optional extra tools (e.g. Hermes adapter wrappers).
        # When ``extra_tools`` is None (the default), this is a no-op and
        # the existing 4-tool behavior is preserved exactly.
        self._extra_tool_names: set[str] = set()
        if extra_tools:
            for tool in extra_tools:
                if tool.name and tool.name not in self._tools:
                    self._tools[tool.name] = tool
                    self._extra_tool_names.add(tool.name)

    async def execute(self, payload: ToolPayload) -> dict[str, Any]:
        """Execute a tool call with full idempotency and safety checks.

        Returns a dict with ``success``, ``tool_name``, ``data``, and
        ``error`` keys.
        """
        # Observability: start a tracing span for this tool execution.
        span = (
            self._tracer.start_span(
                f"tool.{payload.tool_name}",
                ticket_id=str(payload.ticket_id),
                correlation_id=str(payload.execution_id),
            )
            if self._tracer is not None
            else None
        )
        if span is not None:
            span.set_attribute("tool.name", payload.tool_name)
            span.set_attribute("tool.idempotency_key", payload.idempotency_key)

        import time as _time

        _exec_start = _time.monotonic()
        result = await self._execute_inner(payload)
        duration_ms = (_time.monotonic() - _exec_start) * 1000

        # Observability: record tool execution SLO + span status.
        if self._slo is not None:
            self._slo.record_tool_execution(
                tool_name=payload.tool_name,
                succeeded=result.get("success", False),
                duration_ms=duration_ms,
            )
        if span is not None:
            span.set_attribute("tool.duration_ms", duration_ms)
            span.set_status(result.get("success", False))

        return result

    async def _execute_inner(self, payload: ToolPayload) -> dict[str, Any]:
        """Internal execute — the original logic, separated for tracing."""
        # 1. Allow-list enforcement.
        # The base allow-list (ALLOWED_TOOLS) covers the 4 built-in tools.
        # Extra tools (e.g. Hermes wrappers) are admitted by name — they
        # were registered via ``extra_tools`` at construction time.
        if payload.tool_name not in ALLOWED_TOOLS and payload.tool_name not in self._extra_tool_names:
            logger.warning("rejected tool not in allow-list: %s", payload.tool_name)
            return ToolResult(
                success=False,
                tool_name=payload.tool_name,
                error=f"tool '{payload.tool_name}' is not in the allow-list",
            ).to_dict()

        # 1b. CRITICAL: Validate tool arguments against Pydantic schemas to
        # prevent command injection, SSRF, and other injection attacks from
        # malicious LLM outputs or prompt injection.
        from contracts.tool_schemas import TOOL_SCHEMAS

        schema_class = TOOL_SCHEMAS.get(payload.tool_name)
        if schema_class is not None:
            try:
                validated_args = schema_class(**payload.arguments)
                # Replace raw arguments with validated, safe arguments.
                payload = payload.model_copy(
                    update={"arguments": validated_args.model_dump()}
                )
            except Exception as exc:
                logger.error(
                    "tool argument validation failed for %s: %s",
                    payload.tool_name,
                    exc,
                )
                return ToolResult(
                    success=False,
                    tool_name=payload.tool_name,
                    error=f"invalid tool arguments: {exc}",
                ).to_dict()

        # 2. Idempotency: check for an existing completed execution.
        existing = await self._execution_store.get_by_idempotency_key(
            payload.idempotency_key
        )
        if existing is not None and existing.status in ("succeeded", "failed"):
            logger.info(
                "idempotent short-circuit for %s (key=%s, status=%s)",
                payload.tool_name,
                payload.idempotency_key,
                existing.status,
            )
            return ToolResult(
                success=existing.status == "succeeded",
                tool_name=payload.tool_name,
                data=existing.result or {},
                error=existing.error,
            ).to_dict()

        # 3. User confirmation for sensitive tools.
        if payload.tool_name in SENSITIVE_TOOLS:
            confirmed = await self._confirmation_checker.is_confirmed(
                payload.user_id, payload.tool_name, payload.idempotency_key
            )
            if not confirmed:
                logger.info(
                    "tool %s requires user confirmation (key=%s) — not confirmed",
                    payload.tool_name,
                    payload.idempotency_key,
                )
                return ToolResult(
                    success=False,
                    tool_name=payload.tool_name,
                    error="user confirmation required for this action",
                ).to_dict()

        # 4. Rate limiting.
        allowed = await self._rate_limiter.allow(payload.user_id, payload.tool_name)
        if not allowed:
            logger.warning("rate limit exceeded for %s by user %s", payload.tool_name, payload.user_id)
            return ToolResult(
                success=False,
                tool_name=payload.tool_name,
                error="rate limit exceeded — please try again later",
            ).to_dict()

        # 5. Insert pending tool_executions row.
        now = datetime.now(timezone.utc)
        record = ToolExecutionRecord(
            execution_id=payload.execution_id,
            ticket_id=payload.ticket_id,
            user_id=payload.user_id,
            tool_name=payload.tool_name,
            idempotency_key=payload.idempotency_key,
            arguments=payload.arguments,
            oauth_connection_id=payload.oauth_connection_id,
            dry_run=payload.dry_run,
            status="pending",
            started_at=now,
        )
        try:
            await self._execution_store.insert_pending(record)
        except Exception as exc:
            # Idempotency conflict — another worker already inserted this.
            logger.info("idempotency conflict on insert: %s", exc)
            existing = await self._execution_store.get_by_idempotency_key(
                payload.idempotency_key
            )
            if existing and existing.status in ("succeeded", "failed"):
                return ToolResult(
                    success=existing.status == "succeeded",
                    tool_name=payload.tool_name,
                    data=existing.result or {},
                    error=existing.error,
                ).to_dict()
            # If it's pending/running, another worker is executing it.
            return ToolResult(
                success=False,
                tool_name=payload.tool_name,
                error="tool execution is already in progress by another worker",
            ).to_dict()

        # 6. Mark running.
        await self._execution_store.mark_running(payload.execution_id)

        # 7. Retrieve OAuth credentials if needed.
        credential = await self._get_credential(payload)

        # 8. Execute the tool.
        tool = self._tools.get(payload.tool_name)
        if tool is None:
            await self._execution_store.mark_completed(
                payload.execution_id, "failed", None, f"tool {payload.tool_name} not registered"
            )
            return ToolResult(
                success=False,
                tool_name=payload.tool_name,
                error=f"tool {payload.tool_name} not registered",
            ).to_dict()

        result = await tool.execute(payload, credential)

        # 9. Mark completed.
        await self._execution_store.mark_completed(
            payload.execution_id,
            "succeeded" if result.success else "failed",
            result.data if result.success else None,
            result.error,
        )

        return result.to_dict()

    async def _get_credential(self, payload: ToolPayload) -> OAuthCredential | None:
        """Retrieve OAuth credentials, refreshing if necessary.

        Uses a per-connection Redis lock during token refresh to prevent
        concurrent refresh attempts.
        """
        if payload.oauth_connection_id is None:
            # Some tools might not need OAuth (e.g. dry-run), but in practice
            # all 4 tools require it. Return None and let the tool handle it.
            return None

        # Determine provider from tool name.
        provider = "github" if "github" in payload.tool_name else "gmail"

        credential = await self._oauth_store.get_credential(
            payload.user_id, provider
        )
        if credential is None:
            logger.warning("no OAuth credential found for user %s, provider %s", payload.user_id, provider)
            return None

        # Check if the access token needs refreshing.
        if self._needs_refresh(credential):
            await self._refresh_with_lock(credential)

        return credential

    @staticmethod
    def _needs_refresh(credential: OAuthCredential) -> bool:
        """Return True if the access token is expired or about to expire."""
        if credential.access_token is None:
            return True
        if credential.access_token_expires_at is None:
            return True
        threshold = datetime.now(timezone.utc) + timedelta(
            seconds=TOKEN_REFRESH_THRESHOLD_SECONDS
        )
        return credential.access_token_expires_at <= threshold

    async def _refresh_with_lock(self, credential: OAuthCredential) -> None:
        """Refresh the OAuth token under a per-connection Redis lock."""
        if credential.refresh_token_encrypted is None:
            logger.warning(
                "cannot refresh token for connection %s — no refresh token",
                credential.connection_id,
            )
            return

        lock = OAuthConnectionLock(self._redis, credential.connection_id)
        if not await lock.acquire():
            logger.info(
                "OAuth lock for connection %s is held — waiting briefly",
                credential.connection_id,
            )
            # Another worker is refreshing. Wait and re-read the credential.
            import asyncio

            await asyncio.sleep(0.5)
            refreshed = await self._oauth_store.get_credential(
                credential.provider_user_id and UUID(credential.provider_user_id) or credential.connection_id,
                credential.provider,
            )
            if refreshed:
                credential.access_token = refreshed.access_token
                credential.access_token_expires_at = refreshed.access_token_expires_at
            return

        try:
            refreshed = await self._oauth_store.refresh_token(
                credential.connection_id,
                credential.refresh_token_encrypted,
            )
            credential.access_token = refreshed.access_token
            credential.access_token_expires_at = refreshed.access_token_expires_at
            logger.info("refreshed OAuth token for connection %s", credential.connection_id)
        except Exception:
            logger.exception(
                "failed to refresh OAuth token for connection %s",
                credential.connection_id,
            )
        finally:
            await lock.release()
