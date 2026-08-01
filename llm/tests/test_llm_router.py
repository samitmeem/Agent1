"""Tests for the LLM router — Qwen primary, DeepSeek fallback.

Covers:
- Successful primary (Qwen/vLLM) response.
- Timeout on primary → fallback to DeepSeek.
- Invalid JSON on primary → fallback to DeepSeek.
- Pydantic validation failure on primary → fallback.
- Disallowed tool name on primary → fallback.
- Both routes fail → safe error decision.
- ``model_attempt`` recorded for every attempt (success and failure).
- ``response_text`` present on ``respond`` action.
- Tool payload preserved with correct ``execution_id`` / ``idempotency_key``.
- Markdown-fenced JSON parsed correctly.
- ``build_router`` returns an ``LLMRouter`` with litellm ``acompletion``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
from contracts.models import AgentDecision, TicketState, ToolPayload
from llm.router import (
    ALLOWED_TOOLS,
    FallbackTriggered,
    LLMRouter,
    build_router,
)


# ---------------------------------------------------------------------------
# Helpers — mock acompletion and attempt recorder.
# ---------------------------------------------------------------------------
class MockCompletionResponse:
    """Mimics litellm's ModelResponse shape."""

    class _Message:
        def __init__(self, content: str):
            self.content = content

    class _Choice:
        def __init__(self, content: str):
            self.message = MockCompletionResponse._Message(content)

    class _Usage:
        def __init__(self, prompt_tokens: int, completion_tokens: int):
            self.prompt_tokens = prompt_tokens
            self.completion_tokens = completion_tokens

        def model_dump(self) -> dict[str, int]:
            return {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            }

    def __init__(self, content: str, usage: dict | None = None):
        self.choices = [MockCompletionResponse._Choice(content)]
        if usage:
            self.usage = MockCompletionResponse._Usage(
                usage.get("prompt_tokens", 10),
                usage.get("completion_tokens", 20),
            )
        else:
            self.usage = MockCompletionResponse._Usage(10, 20)


class MockACompletion:
    """Configurable async mock for ``litellm.acompletion``.

    ``side_effects`` maps model name → callable or exception.
    If a model isn't in the map, raises ``ValueError``.
    """

    def __init__(self, side_effects: dict[str, Any] | None = None):
        self.side_effects = side_effects or {}
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, model: str, messages: list[dict], **kwargs) -> Any:
        self.calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        if model not in self.side_effects:
            raise ValueError(f"unexpected model: {model}")
        effect = self.side_effects[model]
        if isinstance(effect, Exception):
            raise effect
        if callable(effect):
            return effect()
        return effect


class MockAttemptRecorder:
    """Records all ``model_attempt`` calls."""

    def __init__(self):
        self.attempts: list[dict[str, Any]] = []

    async def record(
        self,
        ticket_id,
        run_id,
        provider,
        model,
        latency_ms,
        error_class,
        error_message,
        correlation_id,
        token_usage,
        succeeded,
    ) -> None:
        self.attempts.append({
            "ticket_id": ticket_id,
            "run_id": run_id,
            "provider": provider,
            "model": model,
            "latency_ms": latency_ms,
            "error_class": error_class,
            "error_message": error_message,
            "correlation_id": correlation_id,
            "token_usage": token_usage,
            "succeeded": succeeded,
        })


def _make_ticket_state() -> TicketState:
    from datetime import datetime, timezone

    return TicketState(
        ticket_id=uuid4(),
        user_id=uuid4(),
        run_id=uuid4(),
        updated_at=datetime.now(timezone.utc),
    )


def _make_respond_json(text: str = "Here is your answer.") -> str:
    return json.dumps({"action": "respond", "response_text": text, "tool": None})


def _make_tool_json(
    ticket_id: Any,
    user_id: Any,
    tool_name: str = "github.create_issue",
    idempotency_key: str = "idem-key-0123456789abcdef",
) -> str:
    return json.dumps({
        "action": "call_tool",
        "response_text": None,
        "tool": {
            "execution_id": str(uuid4()),
            "ticket_id": str(ticket_id),
            "user_id": str(user_id),
            "tool_name": tool_name,
            "idempotency_key": idempotency_key,
            "arguments": {"repository": "test/repo", "title": "Bug", "body": "Login fails"},
            "oauth_connection_id": None,
            "dry_run": False,
        },
    })


def _make_oauth_json(ticket_id: Any, user_id: Any) -> str:
    return json.dumps({
        "action": "request_oauth",
        "response_text": None,
        "tool": {
            "execution_id": str(uuid4()),
            "ticket_id": str(ticket_id),
            "user_id": str(user_id),
            "tool_name": "github.create_issue",
            "idempotency_key": "idem-key-0123456789abcdef",
            "arguments": {"repository": "test/repo", "title": "Bug", "body": "Login fails"},
            "oauth_connection_id": None,
            "dry_run": False,
        },
    })


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------
class TestBuildRouter:
    def test_build_router_returns_llm_router(self):
        router = build_router()
        assert isinstance(router, LLMRouter)

    def test_build_router_with_injected_acompletion(self):
        mock = MockACompletion()
        router = build_router(acompletion=mock)
        assert router._acompletion is mock


class TestSuccessfulPrimary:
    """Qwen/vLLM returns a valid response — no fallback needed."""

    @pytest.mark.asyncio
    async def test_primary_respond_success(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(_make_respond_json("Hello!")),
        })
        recorder = MockAttemptRecorder()
        router = LLMRouter(acompletion=mock_ac, attempt_recorder=recorder)

        decision = await router.decide(ts, [{"role": "user", "text": "Hi"}])

        assert decision.action == "respond"
        assert decision.response_text == "Hello!"
        assert decision.tool is None
        # Only primary was called.
        assert len(mock_ac.calls) == 1
        assert mock_ac.calls[0]["model"] == "qwen2.5-14b-instruct"
        # Primary attempt recorded as success.
        assert len(recorder.attempts) == 1
        assert recorder.attempts[0]["provider"] == "vllm"
        assert recorder.attempts[0]["succeeded"] is True

    @pytest.mark.asyncio
    async def test_primary_tool_call_success(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(
                _make_tool_json(ts.ticket_id, ts.user_id)
            ),
        })
        recorder = MockAttemptRecorder()
        router = LLMRouter(acompletion=mock_ac, attempt_recorder=recorder)

        decision = await router.decide(ts, [{"role": "user", "text": "Create an issue"}])

        assert decision.action == "call_tool"
        assert decision.tool is not None
        assert decision.tool.tool_name == "github.create_issue"
        assert decision.tool.ticket_id == ts.ticket_id
        assert decision.tool.user_id == ts.user_id
        assert len(recorder.attempts) == 1
        assert recorder.attempts[0]["succeeded"] is True

    @pytest.mark.asyncio
    async def test_primary_oauth_request_success(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(
                _make_oauth_json(ts.ticket_id, ts.user_id)
            ),
        })
        router = LLMRouter(acompletion=mock_ac)

        decision = await router.decide(ts, [{"role": "user", "text": "Connect GitHub"}])

        assert decision.action == "request_oauth"
        assert decision.tool is not None

    @pytest.mark.asyncio
    async def test_primary_uses_vllm_base_url_and_timeout(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(_make_respond_json()),
        })
        router = LLMRouter(
            acompletion=mock_ac,
            vllm_base_url="http://test-vllm:9999/v1",
        )
        await router.decide(ts, [{"role": "user", "text": "Hi"}])
        kwargs = mock_ac.calls[0]["kwargs"]
        assert kwargs["api_base"] == "http://test-vllm:9999/v1"
        assert kwargs["timeout"] == 12


class TestTimeoutFallback:
    """Primary times out → fallback to DeepSeek."""

    @pytest.mark.asyncio
    async def test_timeout_triggers_fallback(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": asyncio.TimeoutError("vLLM timed out"),
            "deepseek/deepseek-chat": MockCompletionResponse(_make_respond_json("Fallback OK")),
        })
        recorder = MockAttemptRecorder()
        router = LLMRouter(acompletion=mock_ac, attempt_recorder=recorder)

        decision = await router.decide(ts, [{"role": "user", "text": "Hi"}])

        assert decision.response_text == "Fallback OK"
        assert decision.fallback_reason is not None
        assert "timed out" in decision.fallback_reason.lower() or "timeout" in decision.fallback_reason.lower()
        # Two attempts recorded: primary fail, fallback success.
        assert len(recorder.attempts) == 2
        assert recorder.attempts[0]["provider"] == "vllm"
        assert recorder.attempts[0]["succeeded"] is False
        assert "TimeoutError" in recorder.attempts[0]["error_class"]
        assert recorder.attempts[1]["provider"] == "deepseek"
        assert recorder.attempts[1]["succeeded"] is True

    @pytest.mark.asyncio
    async def test_connection_error_triggers_fallback(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": ConnectionError("vLLM unreachable"),
            "deepseek/deepseek-chat": MockCompletionResponse(_make_respond_json("DeepSeek OK")),
        })
        router = LLMRouter(acompletion=mock_ac)
        decision = await router.decide(ts, [{"role": "user", "text": "Hi"}])
        assert decision.response_text == "DeepSeek OK"
        assert decision.fallback_reason is not None


class TestInvalidJSONFallback:
    """Primary returns invalid JSON → fallback to DeepSeek."""

    @pytest.mark.asyncio
    async def test_invalid_json_triggers_fallback(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse("not json at all {{{"),
            "deepseek/deepseek-chat": MockCompletionResponse(_make_respond_json("Fixed")),
        })
        recorder = MockAttemptRecorder()
        router = LLMRouter(acompletion=mock_ac, attempt_recorder=recorder)

        decision = await router.decide(ts, [{"role": "user", "text": "Hi"}])

        assert decision.response_text == "Fixed"
        assert len(recorder.attempts) == 2
        assert recorder.attempts[0]["error_class"] == "JSONDecodeError"

    @pytest.mark.asyncio
    async def test_empty_response_triggers_fallback(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(""),
            "deepseek/deepseek-chat": MockCompletionResponse(_make_respond_json("Got it")),
        })
        recorder = MockAttemptRecorder()
        router = LLMRouter(acompletion=mock_ac, attempt_recorder=recorder)

        decision = await router.decide(ts, [{"role": "user", "text": "Hi"}])

        assert decision.response_text == "Got it"
        assert recorder.attempts[0]["error_class"] == "EmptyResponse"


class TestValidationFailureFallback:
    """Primary returns valid JSON but fails Pydantic validation → fallback."""

    @pytest.mark.asyncio
    async def test_missing_response_text_on_respond_triggers_fallback(self):
        ts = _make_ticket_state()
        bad_json = json.dumps({"action": "respond", "response_text": None, "tool": None})
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(bad_json),
            "deepseek/deepseek-chat": MockCompletionResponse(_make_respond_json("OK")),
        })
        recorder = MockAttemptRecorder()
        router = LLMRouter(acompletion=mock_ac, attempt_recorder=recorder)

        decision = await router.decide(ts, [{"role": "user", "text": "Hi"}])

        assert decision.response_text == "OK"
        assert recorder.attempts[0]["error_class"] == "MissingResponseText"

    @pytest.mark.asyncio
    async def test_missing_tool_on_call_tool_triggers_fallback(self):
        ts = _make_ticket_state()
        bad_json = json.dumps({"action": "call_tool", "response_text": None, "tool": None})
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(bad_json),
            "deepseek/deepseek-chat": MockCompletionResponse(_make_respond_json("OK")),
        })
        recorder = MockAttemptRecorder()
        router = LLMRouter(acompletion=mock_ac, attempt_recorder=recorder)

        decision = await router.decide(ts, [{"role": "user", "text": "Hi"}])
        assert recorder.attempts[0]["error_class"] == "MissingTool"

    @pytest.mark.asyncio
    async def test_disallowed_tool_name_triggers_fallback(self):
        ts = _make_ticket_state()
        bad_tool_json = json.dumps({
            "action": "call_tool",
            "response_text": None,
            "tool": {
                "execution_id": str(uuid4()),
                "ticket_id": str(ts.ticket_id),
                "user_id": str(ts.user_id),
                "tool_name": "slack.send_message",
                "idempotency_key": "idem-key-0123456789abcdef",
                "arguments": {"channel": "general"},
                "oauth_connection_id": None,
                "dry_run": False,
            },
        })
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(bad_tool_json),
            "deepseek/deepseek-chat": MockCompletionResponse(_make_respond_json("OK")),
        })
        recorder = MockAttemptRecorder()
        router = LLMRouter(acompletion=mock_ac, attempt_recorder=recorder)

        decision = await router.decide(ts, [{"role": "user", "text": "Hi"}])
        assert recorder.attempts[0]["error_class"] == "InvalidToolName"


class TestBothFail:
    """Both primary and fallback fail → safe error decision."""

    @pytest.mark.asyncio
    async def test_both_fail_returns_safe_error(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": asyncio.TimeoutError("vLLM down"),
            "deepseek/deepseek-chat": ConnectionError("DeepSeek down"),
        })
        recorder = MockAttemptRecorder()
        router = LLMRouter(acompletion=mock_ac, attempt_recorder=recorder)

        decision = await router.decide(ts, [{"role": "user", "text": "Hi"}])

        assert decision.action == "respond"
        assert "unable to process" in decision.response_text.lower()
        # Two failed attempts recorded.
        assert len(recorder.attempts) == 2
        assert all(not a["succeeded"] for a in recorder.attempts)

    @pytest.mark.asyncio
    async def test_both_fail_with_invalid_json(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse("garbage"),
            "deepseek/deepseek-chat": MockCompletionResponse("also garbage"),
        })
        router = LLMRouter(acompletion=mock_ac)
        decision = await router.decide(ts, [{"role": "user", "text": "Hi"}])
        assert decision.action == "respond"
        assert decision.response_text is not None


class TestMarkdownFencedJSON:
    """Model wraps JSON in markdown code fences — should still parse."""

    @pytest.mark.asyncio
    async def test_markdown_fenced_json_parsed(self):
        ts = _make_ticket_state()
        fenced = f"```json\n{_make_respond_json('Fenced!')}\n```"
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(fenced),
        })
        router = LLMRouter(acompletion=mock_ac)
        decision = await router.decide(ts, [{"role": "user", "text": "Hi"}])
        assert decision.response_text == "Fenced!"


class TestModelAttemptRecording:
    """Verify model_attempt rows are written for every attempt."""

    @pytest.mark.asyncio
    async def test_success_records_attempt(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(_make_respond_json()),
        })
        recorder = MockAttemptRecorder()
        router = LLMRouter(acompletion=mock_ac, attempt_recorder=recorder)
        await router.decide(ts, [{"role": "user", "text": "Hi"}])
        assert len(recorder.attempts) == 1
        a = recorder.attempts[0]
        assert a["provider"] == "vllm"
        assert a["model"] == "qwen2.5-14b-instruct"
        assert a["latency_ms"] is not None
        assert a["succeeded"] is True
        assert a["token_usage"] is not None

    @pytest.mark.asyncio
    async def test_failure_records_attempt(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": asyncio.TimeoutError(),
            "deepseek/deepseek-chat": MockCompletionResponse(_make_respond_json()),
        })
        recorder = MockAttemptRecorder()
        router = LLMRouter(acompletion=mock_ac, attempt_recorder=recorder)
        await router.decide(ts, [{"role": "user", "text": "Hi"}])
        assert len(recorder.attempts) == 2
        assert recorder.attempts[0]["succeeded"] is False
        assert recorder.attempts[0]["error_class"] is not None
        assert recorder.attempts[1]["succeeded"] is True

    @pytest.mark.asyncio
    async def test_no_recorder_does_not_crash(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(_make_respond_json()),
        })
        router = LLMRouter(acompletion=mock_ac, attempt_recorder=None)
        decision = await router.decide(ts, [{"role": "user", "text": "Hi"}])
        assert decision.action == "respond"


class TestIdempotencyPreservation:
    """Same execution_id + idempotency_key preserved across fallback."""

    @pytest.mark.asyncio
    async def test_tool_payload_preserved_across_fallback(self):
        ts = _make_ticket_state()
        idem_key = "idem-key-0123456789abcdef"
        tool_json = _make_tool_json(ts.ticket_id, ts.user_id, idempotency_key=idem_key)
        # Primary returns invalid JSON, fallback returns valid tool call.
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse("bad json"),
            "deepseek/deepseek-chat": MockCompletionResponse(tool_json),
        })
        router = LLMRouter(acompletion=mock_ac)
        decision = await router.decide(ts, [{"role": "user", "text": "Create issue"}])
        assert decision.action == "call_tool"
        assert decision.tool is not None
        assert decision.tool.idempotency_key == idem_key


class TestFallbackTimeoutConfig:
    """Verify fallback uses the correct timeout."""

    @pytest.mark.asyncio
    async def test_fallback_uses_25s_timeout(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": asyncio.TimeoutError(),
            "deepseek/deepseek-chat": MockCompletionResponse(_make_respond_json()),
        })
        router = LLMRouter(acompletion=mock_ac)
        await router.decide(ts, [{"role": "user", "text": "Hi"}])
        # Fallback call should have timeout=25.
        fallback_call = mock_ac.calls[1]
        assert fallback_call["kwargs"]["timeout"] == 25

    @pytest.mark.asyncio
    async def test_fallback_uses_deepseek_api_key(self):
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": asyncio.TimeoutError(),
            "deepseek/deepseek-chat": MockCompletionResponse(_make_respond_json()),
        })
        router = LLMRouter(acompletion=mock_ac, deepseek_api_key="test-key-123")
        await router.decide(ts, [{"role": "user", "text": "Hi"}])
        fallback_call = mock_ac.calls[1]
        assert fallback_call["kwargs"]["api_key"] == "test-key-123"


# ---------------------------------------------------------------------------
# Observability integration tests (Task 12).
# ---------------------------------------------------------------------------
class TestLLMObservability:
    @pytest.mark.asyncio
    async def test_slo_tracker_records_successful_primary(self):
        """LLM router records llm_attempts_total on primary success."""
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(_make_respond_json()),
        })

        class RecordingSLO:
            def __init__(self):
                self.llm_calls: list = []

            def record_llm_attempt(self, *, provider, model, succeeded, fallback=False):
                self.llm_calls.append((provider, model, succeeded, fallback))

            def record_model_start(self, ticket_id):
                pass

            def record_ttft(self, ticket_id):
                pass

        slo = RecordingSLO()
        router = LLMRouter(acompletion=mock_ac, slo_tracker=slo)
        await router.decide(ts, [{"role": "user", "text": "Hi"}])

        assert len(slo.llm_calls) == 1
        assert slo.llm_calls[0] == ("vllm", "qwen2.5-14b-instruct", True, False)

    @pytest.mark.asyncio
    async def test_slo_tracker_records_fallback(self):
        """LLM router records both primary failure and fallback success."""
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": asyncio.TimeoutError(),
            "deepseek/deepseek-chat": MockCompletionResponse(_make_respond_json()),
        })

        class RecordingSLO:
            def __init__(self):
                self.llm_calls: list = []

            def record_llm_attempt(self, *, provider, model, succeeded, fallback=False):
                self.llm_calls.append((provider, model, succeeded, fallback))

            def record_model_start(self, ticket_id):
                pass

            def record_ttft(self, ticket_id):
                pass

        slo = RecordingSLO()
        router = LLMRouter(acompletion=mock_ac, slo_tracker=slo)
        await router.decide(ts, [{"role": "user", "text": "Hi"}])

        # Primary failed, fallback succeeded.
        assert len(slo.llm_calls) == 2
        assert slo.llm_calls[0] == ("vllm", "qwen2.5-14b-instruct", False, False)
        assert slo.llm_calls[1] == ("deepseek", "deepseek/deepseek-chat", True, True)

    @pytest.mark.asyncio
    async def test_tracer_span_created_on_attempt(self):
        """LLM router creates a tracing span for each attempt."""
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": MockCompletionResponse(_make_respond_json()),
        })

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
        router = LLMRouter(acompletion=mock_ac, tracer=tracer)
        await router.decide(ts, [{"role": "user", "text": "Hi"}])

        assert len(tracer.spans) == 1
        assert tracer.spans[0].name == "llm.vllm"
        assert tracer.spans[0].attrs["llm.provider"] == "vllm"
        assert tracer.spans[0].status[0] is True  # ok

    @pytest.mark.asyncio
    async def test_tracer_span_records_exception_on_failure(self):
        """LLM router records exception on span when primary fails."""
        ts = _make_ticket_state()
        mock_ac = MockACompletion({
            "qwen2.5-14b-instruct": asyncio.TimeoutError(),
            "deepseek/deepseek-chat": MockCompletionResponse(_make_respond_json()),
        })

        class RecordingTracer:
            def __init__(self):
                self.spans: list = []
                self.exceptions: list = []

            def start_span(self, name, *, ticket_id=None, correlation_id=None):
                span = RecordingSpan(name)
                self.spans.append(span)
                return span

        class RecordingSpan:
            def __init__(self, name):
                self.name = name
                self.attrs: dict = {}
                self.status: tuple | None = None
                self.exc = None

            def set_attribute(self, key, value):
                self.attrs[key] = value

            def set_status(self, ok, description=None):
                self.status = (ok, description)

            def record_exception(self, exc):
                self.exc = exc

        tracer = RecordingTracer()
        router = LLMRouter(acompletion=mock_ac, tracer=tracer)
        await router.decide(ts, [{"role": "user", "text": "Hi"}])

        # Two spans: primary (failed) + fallback (succeeded).
        assert len(tracer.spans) == 2
        # Primary span recorded the exception.
        assert tracer.spans[0].exc is not None
        assert tracer.spans[0].status[0] is False  # error
        # Fallback span succeeded.
        assert tracer.spans[1].status[0] is True
