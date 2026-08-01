"""Tests for tool execution, OAuth connectors, and the ToolExecutor.

Covers:
- All 4 tools (GitHub create/comment, Gmail send/search) with mocked httpx.
- Allow-list enforcement.
- Idempotency: short-circuit on existing completed execution.
- Idempotency: conflict on insert_pending.
- User confirmation for sensitive tools (gmail.send_email).
- Rate limiting.
- OAuth credential retrieval and token refresh under Redis lock.
- Dry-run mode.
- Timeout and HTTP error handling.
- tool_executions lifecycle (pending → running → succeeded/failed).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx
import pytest
from contracts.models import ToolPayload
from worker.tools.base import (
    ALLOWED_TOOLS,
    OAuthCredential,
    OAuthCredentialStoreProtocol,
    RateLimiterProtocol,
    SENSITIVE_TOOLS,
    ToolExecutionRecord,
    ToolExecutionStoreProtocol,
    ToolResult,
    UserConfirmationProtocol,
)
from worker.tools.executor import ToolExecutor
from worker.tools.github import GitHubCreateIssue, GitHubCommentIssue
from worker.tools.gmail import GmailSearchMessages, GmailSendEmail


# ---------------------------------------------------------------------------
# Mock implementations.
# ---------------------------------------------------------------------------
class MockHttpxResponse:
    """Mimics httpx.Response."""

    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self
            )


class MockAsyncClient:
    """Mock httpx.AsyncClient that returns canned responses per URL+method."""

    def __init__(self, responses: dict[str, MockHttpxResponse] | None = None):
        self._responses = responses or {}
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs) -> MockHttpxResponse:
        self.calls.append({"method": "POST", "url": url, "kwargs": kwargs})
        key = f"POST:{url}"
        if key in self._responses:
            return self._responses[key]
        return MockHttpxResponse(200, {"id": 1, "html_url": "https://example.com/1"})

    async def get(self, url: str, **kwargs) -> MockHttpxResponse:
        self.calls.append({"method": "GET", "url": url, "kwargs": kwargs})
        key = f"GET:{url}"
        if key in self._responses:
            return self._responses[key]
        return MockHttpxResponse(200, {"messages": [{"id": "msg1"}]})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockExecutionStore:
    """In-memory tool_executions store."""

    def __init__(self, existing: ToolExecutionRecord | None = None):
        self._records: dict[str, ToolExecutionRecord] = {}
        self._by_id: dict[Any, ToolExecutionRecord] = {}
        if existing:
            self._records[existing.idempotency_key] = existing
            self._by_id[existing.execution_id] = existing
        self.insert_count = 0
        self.running_count = 0
        self.completed_count = 0

    async def insert_pending(self, record: ToolExecutionRecord) -> None:
        if record.idempotency_key in self._records:
            raise ValueError(f"idempotency_key {record.idempotency_key} already exists")
        self.insert_count += 1
        self._records[record.idempotency_key] = record
        self._by_id[record.execution_id] = record

    async def mark_running(self, execution_id) -> None:
        self.running_count += 1
        if execution_id in self._by_id:
            self._by_id[execution_id].status = "running"

    async def mark_completed(self, execution_id, status, result, error) -> None:
        self.completed_count += 1
        if execution_id in self._by_id:
            rec = self._by_id[execution_id]
            rec.status = status
            rec.result = result
            rec.error = error
            rec.completed_at = datetime.now(timezone.utc)

    async def get_by_idempotency_key(self, idempotency_key) -> ToolExecutionRecord | None:
        return self._records.get(idempotency_key)


class MockOAuthStore:
    """In-memory OAuth credential store."""

    def __init__(self, credential: OAuthCredential | None = None):
        self._credential = credential
        self.refresh_count = 0

    async def get_credential(self, user_id, provider) -> OAuthCredential | None:
        return self._credential

    async def refresh_token(self, connection_id, refresh_token_encrypted) -> OAuthCredential:
        self.refresh_count += 1
        if self._credential:
            self._credential.access_token = f"refreshed-token-{self.refresh_count}"
            self._credential.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        return self._credential

    async def update_access_token(self, connection_id, access_token, expires_at) -> None:
        pass


class MockConfirmationChecker:
    """Configurable user confirmation checker."""

    def __init__(self, confirmed: bool = True):
        self._confirmed = confirmed
        self.check_count = 0

    async def is_confirmed(self, user_id, tool_name, idempotency_key) -> bool:
        self.check_count += 1
        return self._confirmed


class MockRateLimiter:
    """Configurable rate limiter."""

    def __init__(self, allowed: bool = True):
        self._allowed = allowed
        self.check_count = 0

    async def allow(self, user_id, tool_name) -> bool:
        self.check_count += 1
        return self._allowed


class MockRedis:
    """Minimal Redis mock for lock tests."""

    def __init__(self):
        self._store: dict[str, bytes] = {}

    async def set(self, key, value, nx=False, ex=None, **kwargs):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def get(self, key):
        return self._store.get(key)

    async def delete(self, key):
        return self._store.pop(key, None) is not None

    async def incr(self, key):
        val = int(self._store.get(key, b"0")) + 1
        self._store[key] = str(val).encode()
        return val

    async def expire(self, key, seconds):
        return key in self._store

    async def eval(self, script, numkeys, *args):
        key = args[0]
        token = args[1]
        stored = self._store.get(key)
        if stored == token:
            if "DEL" in script:
                self._store.pop(key, None)
            return True
        return False


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _make_credential(
    *,
    access_token: str = "ghp_testtoken123",
    expired: bool = False,
    connection_id: UUID | None = None,
) -> OAuthCredential:
    return OAuthCredential(
        connection_id=connection_id or uuid4(),
        provider="github",
        access_token=access_token,
        refresh_token_encrypted=b"encrypted-refresh-token",
        access_token_expires_at=(
            datetime.now(timezone.utc) - timedelta(seconds=10)
            if expired
            else datetime.now(timezone.utc) + timedelta(hours=1)
        ),
        scopes=["repo"],
    )


def _make_tool_payload(
    *,
    tool_name: str = "github.create_issue",
    arguments: dict | None = None,
    dry_run: bool = False,
    oauth_connection_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> ToolPayload:
    if arguments is None:
        arguments = {
            "repository": "test/repo",
            "title": "Bug report",
            "body": "Login fails on Safari",
        }
    return ToolPayload(
        execution_id=uuid4(),
        ticket_id=uuid4(),
        user_id=uuid4(),
        tool_name=tool_name,
        idempotency_key=idempotency_key or "idem-key-0123456789abcdef",
        arguments=arguments,
        oauth_connection_id=oauth_connection_id or uuid4(),
        dry_run=dry_run,
    )


def _make_executor(
    *,
    http_client: MockAsyncClient | None = None,
    execution_store: MockExecutionStore | None = None,
    oauth_store: MockOAuthStore | None = None,
    confirmation_checker: MockConfirmationChecker | None = None,
    rate_limiter: MockRateLimiter | None = None,
    redis: MockRedis | None = None,
) -> ToolExecutor:
    return ToolExecutor(
        execution_store=execution_store or MockExecutionStore(),
        oauth_store=oauth_store or MockOAuthStore(_make_credential()),
        confirmation_checker=confirmation_checker or MockConfirmationChecker(),
        rate_limiter=rate_limiter or MockRateLimiter(),
        redis=redis or MockRedis(),
        http_client=http_client or MockAsyncClient(),
    )


# ---------------------------------------------------------------------------
# Allow-list tests.
# ---------------------------------------------------------------------------
class TestAllowList:
    def test_all_four_tools_in_allow_list(self):
        assert "github.create_issue" in ALLOWED_TOOLS
        assert "github.comment_issue" in ALLOWED_TOOLS
        assert "gmail.send_email" in ALLOWED_TOOLS
        assert "gmail.search_messages" in ALLOWED_TOOLS

    def test_allow_list_is_frozen(self):
        assert isinstance(ALLOWED_TOOLS, frozenset)

    def test_sensitive_tools_includes_send_email(self):
        assert "gmail.send_email" in SENSITIVE_TOOLS

    @pytest.mark.asyncio
    async def test_rejected_tool_not_in_allow_list(self):
        # ToolPayload uses a Literal type that rejects unknown tool names at
        # construction time.  We bypass Pydantic to test the executor's own
        # allow-list enforcement.
        payload = _make_tool_payload()
        object.__setattr__(payload, "tool_name", "slack.send_message")
        executor = _make_executor()
        result = await executor.execute(payload)
        assert result["success"] is False
        assert "not in the allow-list" in result["error"]


# ---------------------------------------------------------------------------
# GitHub tool tests.
# ---------------------------------------------------------------------------
class TestGitHubCreateIssue:
    @pytest.mark.asyncio
    async def test_create_issue_success(self):
        http = MockAsyncClient(responses={
            "POST:https://api.github.com/repos/test/repo/issues": MockHttpxResponse(
                201, {"number": 42, "html_url": "https://github.com/test/repo/issues/42"}
            ),
        })
        executor = _make_executor(http_client=http)
        payload = _make_tool_payload(tool_name="github.create_issue")
        result = await executor.execute(payload)

        assert result["success"] is True
        assert result["data"]["issue_number"] == 42
        assert result["data"]["repository"] == "test/repo"
        # Verify the HTTP call was made with the OAuth token.
        assert len(http.calls) == 1
        headers = http.calls[0]["kwargs"]["headers"]
        assert headers["Authorization"] == "Bearer ghp_testtoken123"

    @pytest.mark.asyncio
    async def test_create_issue_dry_run(self):
        executor = _make_executor()
        payload = _make_tool_payload(tool_name="github.create_issue", dry_run=True)
        result = await executor.execute(payload)
        assert result["success"] is True
        assert result["data"]["dry_run"] is True

    @pytest.mark.asyncio
    async def test_create_issue_missing_repository(self):
        executor = _make_executor()
        payload = _make_tool_payload(
            tool_name="github.create_issue",
            arguments={"title": "Bug", "body": "desc"},
        )
        result = await executor.execute(payload)
        assert result["success"] is False
        assert "repository" in result["error"]

    @pytest.mark.asyncio
    async def test_create_issue_http_error(self):
        http = MockAsyncClient(responses={
            "POST:https://api.github.com/repos/test/repo/issues": MockHttpxResponse(403),
        })
        executor = _make_executor(http_client=http)
        payload = _make_tool_payload(tool_name="github.create_issue")
        result = await executor.execute(payload)
        assert result["success"] is False
        assert "http" in result["error"].lower()


class TestGitHubCommentIssue:
    @pytest.mark.asyncio
    async def test_comment_issue_success(self):
        http = MockAsyncClient(responses={
            "POST:https://api.github.com/repos/test/repo/issues/42/comments": MockHttpxResponse(
                201, {"id": 999, "html_url": "https://github.com/test/repo/issues/42#issuecomment-999"}
            ),
        })
        executor = _make_executor(http_client=http)
        payload = _make_tool_payload(
            tool_name="github.comment_issue",
            arguments={"repository": "test/repo", "issue_number": 42, "body": "Looks good"},
        )
        result = await executor.execute(payload)
        assert result["success"] is True
        assert result["data"]["comment_id"] == 999
        assert result["data"]["issue_number"] == 42

    @pytest.mark.asyncio
    async def test_comment_issue_missing_issue_number(self):
        executor = _make_executor()
        payload = _make_tool_payload(
            tool_name="github.comment_issue",
            arguments={"repository": "test/repo", "body": "comment"},
        )
        result = await executor.execute(payload)
        assert result["success"] is False
        assert "issue_number" in result["error"]


# ---------------------------------------------------------------------------
# Gmail tool tests.
# ---------------------------------------------------------------------------
class TestGmailSendEmail:
    @pytest.mark.asyncio
    async def test_send_email_success(self):
        http = MockAsyncClient(responses={
            "POST:https://gmail.googleapis.com/gmail/v1/users/me/messages/send": MockHttpxResponse(
                200, {"id": "msg123", "threadId": "t1"}
            ),
        })
        executor = _make_executor(http_client=http)
        payload = _make_tool_payload(
            tool_name="gmail.send_email",
            arguments={"to": "user@example.com", "subject": "Test", "body": "Hello"},
        )
        result = await executor.execute(payload)
        assert result["success"] is True
        assert result["data"]["message_id"] == "msg123"
        assert result["data"]["to"] == "user@example.com"

    @pytest.mark.asyncio
    async def test_send_email_missing_fields(self):
        executor = _make_executor()
        payload = _make_tool_payload(
            tool_name="gmail.send_email",
            arguments={"to": "user@example.com"},
        )
        result = await executor.execute(payload)
        assert result["success"] is False
        assert "subject" in result["error"] or "body" in result["error"]

    @pytest.mark.asyncio
    async def test_send_email_dry_run(self):
        executor = _make_executor()
        payload = _make_tool_payload(
            tool_name="gmail.send_email",
            arguments={"to": "user@example.com", "subject": "Test", "body": "Hello"},
            dry_run=True,
        )
        result = await executor.execute(payload)
        assert result["success"] is True
        assert result["data"]["dry_run"] is True


class TestGmailSearchMessages:
    @pytest.mark.asyncio
    async def test_search_success(self):
        http = MockAsyncClient(responses={
            "GET:https://gmail.googleapis.com/gmail/v1/users/me/messages": MockHttpxResponse(
                200, {"messages": [{"id": "m1"}, {"id": "m2"}], "resultSizeEstimate": 2}
            ),
        })
        executor = _make_executor(http_client=http)
        payload = _make_tool_payload(
            tool_name="gmail.search_messages",
            arguments={"query": "from:boss@example.com", "max_results": 5},
        )
        result = await executor.execute(payload)
        assert result["success"] is True
        assert result["data"]["message_ids"] == ["m1", "m2"]
        assert result["data"]["query"] == "from:boss@example.com"

    @pytest.mark.asyncio
    async def test_search_missing_query(self):
        executor = _make_executor()
        payload = _make_tool_payload(
            tool_name="gmail.search_messages",
            arguments={"max_results": 5},
        )
        result = await executor.execute(payload)
        assert result["success"] is False
        assert "query" in result["error"]


# ---------------------------------------------------------------------------
# Idempotency tests.
# ---------------------------------------------------------------------------
class TestIdempotency:
    @pytest.mark.asyncio
    async def test_completed_execution_short_circuits(self):
        payload = _make_tool_payload(idempotency_key="idem-key-0123456789abcdef")
        existing = ToolExecutionRecord(
            execution_id=payload.execution_id,
            ticket_id=payload.ticket_id,
            user_id=payload.user_id,
            tool_name=payload.tool_name,
            idempotency_key=payload.idempotency_key,
            arguments=payload.arguments,
            oauth_connection_id=payload.oauth_connection_id,
            dry_run=False,
            status="succeeded",
            result={"issue_number": 99},
        )
        store = MockExecutionStore(existing=existing)
        executor = _make_executor(execution_store=store)
        result = await executor.execute(payload)

        # Should return the existing result without making an HTTP call.
        assert result["success"] is True
        assert result["data"]["issue_number"] == 99
        assert store.insert_count == 0  # No new insert.

    @pytest.mark.asyncio
    async def test_failed_execution_short_circuits(self):
        payload = _make_tool_payload(idempotency_key="idem-key-0123456789abcdef")
        existing = ToolExecutionRecord(
            execution_id=payload.execution_id,
            ticket_id=payload.ticket_id,
            user_id=payload.user_id,
            tool_name=payload.tool_name,
            idempotency_key=payload.idempotency_key,
            arguments=payload.arguments,
            oauth_connection_id=payload.oauth_connection_id,
            dry_run=False,
            status="failed",
            error="previous failure",
        )
        store = MockExecutionStore(existing=existing)
        executor = _make_executor(execution_store=store)
        result = await executor.execute(payload)
        assert result["success"] is False
        assert result["error"] == "previous failure"

    @pytest.mark.asyncio
    async def test_insert_conflict_returns_in_progress(self):
        store = MockExecutionStore()
        # Pre-populate with a pending record.
        payload = _make_tool_payload(idempotency_key="idem-key-0123456789abcdef")
        pending = ToolExecutionRecord(
            execution_id=payload.execution_id,
            ticket_id=payload.ticket_id,
            user_id=payload.user_id,
            tool_name=payload.tool_name,
            idempotency_key=payload.idempotency_key,
            arguments=payload.arguments,
            oauth_connection_id=payload.oauth_connection_id,
            dry_run=False,
            status="pending",
        )
        store._records[payload.idempotency_key] = pending
        store._by_id[payload.execution_id] = pending

        executor = _make_executor(execution_store=store)
        result = await executor.execute(payload)
        assert result["success"] is False
        assert "in progress" in result["error"]


# ---------------------------------------------------------------------------
# User confirmation tests.
# ---------------------------------------------------------------------------
class TestUserConfirmation:
    @pytest.mark.asyncio
    async def test_send_email_requires_confirmation(self):
        executor = _make_executor(
            confirmation_checker=MockConfirmationChecker(confirmed=False),
        )
        payload = _make_tool_payload(
            tool_name="gmail.send_email",
            arguments={"to": "user@example.com", "subject": "Test", "body": "Hello"},
        )
        result = await executor.execute(payload)
        assert result["success"] is False
        assert "confirmation required" in result["error"]

    @pytest.mark.asyncio
    async def test_send_email_with_confirmation_succeeds(self):
        http = MockAsyncClient(responses={
            "POST:https://gmail.googleapis.com/gmail/v1/users/me/messages/send": MockHttpxResponse(
                200, {"id": "msg1", "threadId": "t1"}
            ),
        })
        executor = _make_executor(
            http_client=http,
            confirmation_checker=MockConfirmationChecker(confirmed=True),
        )
        payload = _make_tool_payload(
            tool_name="gmail.send_email",
            arguments={"to": "user@example.com", "subject": "Test", "body": "Hello"},
        )
        result = await executor.execute(payload)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_non_sensitive_tool_skips_confirmation(self):
        http = MockAsyncClient(responses={
            "POST:https://api.github.com/repos/test/repo/issues": MockHttpxResponse(
                201, {"number": 1, "html_url": "url"}
            ),
        })
        checker = MockConfirmationChecker(confirmed=False)
        executor = _make_executor(http_client=http, confirmation_checker=checker)
        payload = _make_tool_payload(tool_name="github.create_issue")
        result = await executor.execute(payload)
        # github.create_issue is not sensitive — confirmation not checked.
        assert result["success"] is True
        assert checker.check_count == 0


# ---------------------------------------------------------------------------
# Rate limiting tests.
# ---------------------------------------------------------------------------
class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self):
        executor = _make_executor(rate_limiter=MockRateLimiter(allowed=False))
        payload = _make_tool_payload(tool_name="github.create_issue")
        result = await executor.execute(payload)
        assert result["success"] is False
        assert "rate limit" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_rate_limit_allowed(self):
        http = MockAsyncClient(responses={
            "POST:https://api.github.com/repos/test/repo/issues": MockHttpxResponse(
                201, {"number": 1, "html_url": "url"}
            ),
        })
        executor = _make_executor(
            http_client=http,
            rate_limiter=MockRateLimiter(allowed=True),
        )
        payload = _make_tool_payload(tool_name="github.create_issue")
        result = await executor.execute(payload)
        assert result["success"] is True


# ---------------------------------------------------------------------------
# OAuth credential tests.
# ---------------------------------------------------------------------------
class TestOAuthCredentials:
    @pytest.mark.asyncio
    async def test_credential_retrieved_and_used(self):
        cred = _make_credential(access_token="ghp_mytoken456")
        executor = _make_executor(
            oauth_store=MockOAuthStore(cred),
            http_client=MockAsyncClient(responses={
                "POST:https://api.github.com/repos/test/repo/issues": MockHttpxResponse(
                    201, {"number": 1, "html_url": "url"}
                ),
            }),
        )
        payload = _make_tool_payload(tool_name="github.create_issue")
        result = await executor.execute(payload)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_token_refreshed_when_expired(self):
        cred = _make_credential(expired=True)
        oauth_store = MockOAuthStore(cred)
        executor = _make_executor(
            oauth_store=oauth_store,
            http_client=MockAsyncClient(responses={
                "POST:https://api.github.com/repos/test/repo/issues": MockHttpxResponse(
                    201, {"number": 1, "html_url": "url"}
                ),
            }),
        )
        payload = _make_tool_payload(tool_name="github.create_issue")
        result = await executor.execute(payload)
        assert result["success"] is True
        assert oauth_store.refresh_count == 1

    @pytest.mark.asyncio
    async def test_no_credential_returns_error(self):
        executor = _make_executor(oauth_store=MockOAuthStore(credential=None))
        payload = _make_tool_payload(tool_name="github.create_issue")
        result = await executor.execute(payload)
        assert result["success"] is False
        assert "access token" in result["error"].lower() or "credential" in result["error"].lower()


# ---------------------------------------------------------------------------
# tool_executions lifecycle tests.
# ---------------------------------------------------------------------------
class TestToolExecutionsLifecycle:
    @pytest.mark.asyncio
    async def test_pending_inserted_then_running_then_completed(self):
        store = MockExecutionStore()
        executor = _make_executor(
            execution_store=store,
            http_client=MockAsyncClient(responses={
                "POST:https://api.github.com/repos/test/repo/issues": MockHttpxResponse(
                    201, {"number": 1, "html_url": "url"}
                ),
            }),
        )
        payload = _make_tool_payload(tool_name="github.create_issue")
        await executor.execute(payload)

        assert store.insert_count == 1
        assert store.running_count == 1
        assert store.completed_count == 1
        record = await store.get_by_idempotency_key(payload.idempotency_key)
        assert record.status == "succeeded"
        assert record.result is not None
        assert record.result["issue_number"] == 1

    @pytest.mark.asyncio
    async def test_failed_execution_marked(self):
        store = MockExecutionStore()
        executor = _make_executor(
            execution_store=store,
            http_client=MockAsyncClient(responses={
                "POST:https://api.github.com/repos/test/repo/issues": MockHttpxResponse(500),
            }),
        )
        payload = _make_tool_payload(tool_name="github.create_issue")
        await executor.execute(payload)
        record = await store.get_by_idempotency_key(payload.idempotency_key)
        assert record.status == "failed"
        assert record.error is not None


# ---------------------------------------------------------------------------
# OAuth lock tests.
# ---------------------------------------------------------------------------
class TestOAuthConnectionLock:
    @pytest.mark.asyncio
    async def test_lock_acquire_and_release(self):
        from worker.tools.executor import OAuthConnectionLock

        redis = MockRedis()
        conn_id = uuid4()
        lock = OAuthConnectionLock(redis, conn_id)
        assert await lock.acquire() is True
        assert lock.key in redis._store
        await lock.release()
        assert lock.key not in redis._store

    @pytest.mark.asyncio
    async def test_lock_second_acquire_fails(self):
        from worker.tools.executor import OAuthConnectionLock

        redis = MockRedis()
        conn_id = uuid4()
        lock1 = OAuthConnectionLock(redis, conn_id)
        lock2 = OAuthConnectionLock(redis, conn_id)
        assert await lock1.acquire() is True
        assert await lock2.acquire() is False


# ---------------------------------------------------------------------------
# Tool executor observability tests (Task 12).
# ---------------------------------------------------------------------------
class TestToolExecutorObservability:
    @pytest.mark.asyncio
    async def test_slo_tracker_records_tool_execution(self):
        """ToolExecutor records tool_executions_total via SLOTracker."""
        from worker.tools.executor import ToolExecutor

        class RecordingSLO:
            def __init__(self):
                self.tool_calls: list = []

            def record_tool_execution(self, *, tool_name, succeeded, duration_ms=None):
                self.tool_calls.append((tool_name, succeeded, duration_ms))

        slo = RecordingSLO()
        executor = ToolExecutor(
            execution_store=MockExecutionStore(),
            oauth_store=MockOAuthStore(_make_credential()),
            confirmation_checker=MockConfirmationChecker(confirmed=True),
            rate_limiter=MockRateLimiter(allowed=True),
            redis=MockRedis(),
            http_client=MockAsyncClient(),
            slo_tracker=slo,
        )
        payload = _make_tool_payload(tool_name="github.create_issue")
        await executor.execute(payload)

        assert len(slo.tool_calls) == 1
        assert slo.tool_calls[0][0] == "github.create_issue"
        assert slo.tool_calls[0][1] is True  # succeeded

    @pytest.mark.asyncio
    async def test_tracer_span_created_on_execute(self):
        """ToolExecutor creates a tracing span for each execution."""
        from worker.tools.executor import ToolExecutor

        class RecordingTracer:
            def __init__(self):
                self.spans: list = []

            def start_span(self, name, *, ticket_id=None, correlation_id=None):
                span = RecordingSpan(name)
                self.spans.append(span)
                return span

        class RecordingSpan:
            def __init__(self, name):
                self.name = name
                self.attrs: dict = {}
                self.status: tuple | None = None

            def set_attribute(self, key, value):
                self.attrs[key] = value

            def set_status(self, ok, description=None):
                self.status = (ok, description)

            def record_exception(self, exc):
                pass

        tracer = RecordingTracer()
        executor = ToolExecutor(
            execution_store=MockExecutionStore(),
            oauth_store=MockOAuthStore(_make_credential()),
            confirmation_checker=MockConfirmationChecker(confirmed=True),
            rate_limiter=MockRateLimiter(allowed=True),
            redis=MockRedis(),
            http_client=MockAsyncClient(),
            tracer=tracer,
        )
        payload = _make_tool_payload(tool_name="github.create_issue")
        await executor.execute(payload)

        assert len(tracer.spans) == 1
        assert tracer.spans[0].name == "tool.github.create_issue"
        assert tracer.spans[0].attrs["tool.name"] == "github.create_issue"
        assert tracer.spans[0].status[0] is True  # ok
