"""LiteLLM router — Qwen primary, DeepSeek fallback.

Implements ``LLMRouter.decide`` per `AgenticProject.md` §1.8 and §3:

- **Primary**: ``openai/qwen-2.5-14b`` via vLLM (``VLLM_BASE_URL``, timeout 12s).
- **Fallback**: ``deepseek/deepseek-chat`` (``DEEPSEEK_API_KEY``, timeout 25s).
- Uses ``litellm.acompletion`` (async) for both routes.
- Validates every response with the ``AgentDecision`` Pydantic model.
- Records a ``model_attempt`` row for every attempt (success and failure).
- Fallback triggers on: vLLM connection/timeout/overload, empty/incomplete
  response, invalid JSON, Pydantic validation failure, tool name not in
  allow-list, invalid tool arguments, unsafe/unrecognized action.
- If both routes fail: returns a safe error ``AgentDecision`` with
  ``action="respond"`` so the caller can persist the error and set the ticket
  to ``failed``.
- Same ``execution_id`` + ``idempotency_key`` preserved across fallback.

The ``acompletion`` callable is injectable so tests can mock it without
importing litellm.

Observability (Task 12): the router optionally accepts a ``Tracer`` and
``SLOTracker`` to record spans for each LLM attempt and emit
``llm_attempts_total`` counters by provider/model/succeeded/fallback.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import ValidationError

from contracts.models import AgentDecision, ToolPayload

logger = logging.getLogger("llm.router")

# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://vllm:8000/v1")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen2.5-14b-instruct")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek/deepseek-chat")
PRIMARY_TIMEOUT = 12
FALLBACK_TIMEOUT = 25

# Cost control: trim conversation history to the last N turns so we don't
# re-send (and re-bill) the entire transcript on every request.
MAX_CONVERSATION_TURNS = int(os.getenv("MAX_CONVERSATION_TURNS", "2"))

# Cost control: enable prompt caching on the static system prompt prefix.
# Providers that support ``cache_control`` (Anthropic, OpenRouter-routed
# Anthropic, etc.) store the prefix and bill ~10% of the input rate on
# cache hits.  Providers that ignore the field are unaffected.
ENABLE_PROMPT_CACHE = os.getenv("ENABLE_PROMPT_CACHE", "1") == "1"

# Allowed tool names — anything else triggers fallback.
# This is the SINGLE SOURCE OF TRUTH for the base tool allow-list.  The system
# prompt below is built from this set so the schema is never duplicated or
# allowed to drift out of sync with the runtime validation.
# Extra tools (e.g. Hermes wrappers, namespaced ``hermes.*``) are merged in
# at ``LLMRouter`` construction time via the ``extra_allowed_tools`` param.
ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "github.create_issue",
        "github.comment_issue",
        "gmail.send_email",
        "gmail.search_messages",
    }
)


def _build_system_prompt(allowed_tools: frozenset[str]) -> str:
    """Build the system prompt with a tool_name enum from ``allowed_tools``.

    Factored out so the router can rebuild the prompt when extra tools
    (e.g. Hermes wrappers) are merged into the allow-list at construction
    time.  The base case (no extra tools) produces the exact same string as
    the original module-level ``SYSTEM_PROMPT``.
    """
    tool_name_enum = " | ".join(f'"{t}"' for t in sorted(allowed_tools))
    return f"""You are a ticket assistant. Analyze the user's request and respond with a JSON object.

The JSON must have this exact shape:
{{
  "action": "respond" | "call_tool" | "request_oauth",
  "response_text": "<string or null>",
  "tool": {{
    "execution_id": "<uuid string>",
    "ticket_id": "<uuid string>",
    "user_id": "<uuid string>",
    "tool_name": {tool_name_enum},
    "idempotency_key": "<string 16-128 chars>",
    "arguments": {{"<key>": "<value>"}},
    "oauth_connection_id": "<uuid string or null>",
    "dry_run": false
  }}
}}

Rules:
- If action is "respond", set response_text and set tool to null.
- If action is "call_tool" or "request_oauth", set tool and set response_text to null.
- tool.arguments must not be empty.
- Only use tool_name values from the allow-list above.
"""


# Module-level prompt for the default (no-extra-tools) case — preserved for
# backward compatibility with any code that imports ``SYSTEM_PROMPT`` directly.
SYSTEM_PROMPT = _build_system_prompt(ALLOWED_TOOLS)


# ---------------------------------------------------------------------------
# Protocols for dependency injection.
# ---------------------------------------------------------------------------
@runtime_checkable
class ACompletionProtocol(Protocol):
    """Async completion callable — matches ``litellm.acompletion``."""

    async def __call__(
        self,
        model: str,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> Any: ...


@runtime_checkable
class ModelAttemptRecorderProtocol(Protocol):
    """Records a ``model_attempt`` row in PostgreSQL."""

    async def record(
        self,
        ticket_id: Any,
        run_id: Any,
        provider: str,
        model: str,
        latency_ms: int | None,
        error_class: str | None,
        error_message: str | None,
        correlation_id: Any,
        token_usage: dict[str, Any] | None,
        succeeded: bool,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------
class FallbackTriggered(Exception):
    """Raised when the primary route fails and we should try fallback."""

    def __init__(self, reason: str, error_class: str = ""):
        self.reason = reason
        self.error_class = error_class
        super().__init__(reason)


# ---------------------------------------------------------------------------
# LLMRouter.
# ---------------------------------------------------------------------------
class LLMRouter:
    """Qwen-first LLM router with DeepSeek fallback.

    Call ``decide`` with the conversation and ticket metadata.  Returns a
    validated ``AgentDecision``.  If both routes fail, returns a safe error
    decision so the caller can persist it and set the ticket to ``failed``.
    """

    def __init__(
        self,
        *,
        acompletion: ACompletionProtocol | None = None,
        attempt_recorder: ModelAttemptRecorderProtocol | None = None,
        vllm_base_url: str = VLLM_BASE_URL,
        deepseek_api_key: str = DEEPSEEK_API_KEY,
        qwen_model: str = QWEN_MODEL,
        deepseek_model: str = DEEPSEEK_MODEL,
        primary_timeout: int = PRIMARY_TIMEOUT,
        fallback_timeout: int = FALLBACK_TIMEOUT,
        tracer: Any | None = None,
        slo_tracker: Any | None = None,
        extra_allowed_tools: frozenset[str] | None = None,
    ) -> None:
        self._acompletion = acompletion
        self._attempt_recorder = attempt_recorder
        self._vllm_base_url = vllm_base_url
        self._deepseek_api_key = deepseek_api_key
        self._qwen_model = qwen_model
        self._deepseek_model = deepseek_model
        self._primary_timeout = primary_timeout
        self._fallback_timeout = fallback_timeout
        self._tracer = tracer
        self._slo = slo_tracker
        # Dynamic allow-list: base tools + optional extras (e.g. Hermes).
        # When ``extra_allowed_tools`` is None/empty, this is exactly
        # ``ALLOWED_TOOLS`` and the prompt is exactly ``SYSTEM_PROMPT``.
        self._allowed_tools: frozenset[str] = ALLOWED_TOOLS | (extra_allowed_tools or frozenset())
        self._system_prompt: str = _build_system_prompt(self._allowed_tools)

    async def decide(
        self,
        ticket_state: Any,
        conversation: list[dict[str, Any]],
        plan: list[str] | None = None,
    ) -> AgentDecision:
        """Route the LLM request: Qwen first, DeepSeek fallback.

        ``ticket_state`` is a ``contracts.models.TicketState`` (duck-typed
        so the router doesn't hard-import the worker's state).
        """
        ticket_id = ticket_state.ticket_id
        run_id = ticket_state.run_id
        user_id = ticket_state.user_id
        correlation_id = run_id  # use run_id as correlation for tracing

        messages = self._build_messages(conversation, plan, ticket_state)

        # --- Primary attempt: Qwen via vLLM ---
        exc_reason = "unknown primary failure"
        try:
            decision = await self._try_route(
                provider="vllm",
                model=self._qwen_model,
                messages=messages,
                timeout=self._primary_timeout,
                api_base=self._vllm_base_url,
                ticket_id=ticket_id,
                run_id=run_id,
                correlation_id=correlation_id,
                user_id=user_id,
            )
            return decision
        except FallbackTriggered as exc:
            logger.warning(
                "primary route failed, falling back to DeepSeek: %s (%s)",
                exc.reason,
                exc.error_class,
            )
            exc_reason = exc.reason
        except Exception as exc:
            logger.warning("primary route raised %s: %s", type(exc).__name__, exc)
            exc_reason = str(exc)

        # --- Fallback attempt: DeepSeek ---
        try:
            decision = await self._try_route(
                provider="deepseek",
                model=self._deepseek_model,
                messages=messages,
                timeout=self._fallback_timeout,
                api_key=self._deepseek_api_key,
                ticket_id=ticket_id,
                run_id=run_id,
                correlation_id=correlation_id,
                user_id=user_id,
            )
            decision.fallback_reason = f"primary failed: {exc_reason}"
            return decision
        except FallbackTriggered as exc:
            # _try_route already recorded the attempt — just log.
            logger.error("fallback route also failed: %s (%s)", exc.reason, exc.error_class)
        except Exception as exc:
            # Unexpected exception (not a FallbackTriggered) — record it.
            logger.error("fallback route raised %s: %s", type(exc).__name__, exc)
            await self._record_attempt(
                ticket_id, run_id, "deepseek", self._deepseek_model,
                None, type(exc).__name__, str(exc), correlation_id, None, False,
            )

        # --- Both failed: return safe error decision ---
        return AgentDecision(
            action="respond",
            response_text=(
                "I'm sorry, I'm unable to process your request right now. "
                "Please try again or contact support if the issue persists."
            ),
        )

    # -------------------------------------------------------------------
    # Internal helpers.
    # -------------------------------------------------------------------
    def _build_messages(
        self,
        conversation: list[dict[str, Any]],
        plan: list[str] | None,
        ticket_state: Any,
    ) -> list[dict[str, str]]:
        """Build the OpenAI-style messages list for ``acompletion``.

        Cost-control layout (cache-friendly):
        1. Static ``SYSTEM_PROMPT`` (marked ``cache_control`` so supporting
           providers cache it — billed at ~10% on hits).
        2. Static plan (also cacheable when stable across turns).
        3. Trimmed conversation (last ``MAX_CONVERSATION_TURNS`` turns only).
        4. Dynamic ticket context (not cached — changes per request).
        """
        system_msg: dict[str, str] = {"role": "system", "content": self._system_prompt}
        if ENABLE_PROMPT_CACHE:
            # Anthropic / OpenRouter-routed Anthropic recognize this field.
            # Other providers ignore it, so it's safe to always include.
            system_msg["cache_control"] = {"type": "ephemeral"}
        messages: list[dict[str, str]] = [system_msg]

        if plan:
            plan_text = "Plan: " + " -> ".join(plan)
            messages.append({"role": "system", "content": plan_text})

        # Trim conversation to the last N turns to avoid re-billing the
        # entire transcript on every request.
        trimmed = conversation[-MAX_CONVERSATION_TURNS:] if conversation else []
        for msg in trimmed:
            role = msg.get("role", "user")
            text = msg.get("text", "")
            if role in ("user", "assistant", "system"):
                messages.append({"role": role, "content": text})

        # Inject ticket context so the model can populate tool payloads.
        context = (
            f"Ticket ID: {ticket_state.ticket_id}\n"
            f"User ID: {ticket_state.user_id}\n"
            f"Run ID: {ticket_state.run_id}"
        )
        messages.append({"role": "system", "content": context})
        return messages

    async def _try_route(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, str]],
        timeout: int,
        ticket_id: UUID,
        run_id: UUID,
        correlation_id: UUID,
        user_id: UUID,
        api_base: str | None = None,
        api_key: str | None = None,
    ) -> AgentDecision:
        """Attempt one LLM route. Raises ``FallbackTriggered`` on failure."""
        if self._acompletion is None:
            raise FallbackTriggered("no acompletion callable configured", "ConfigError")

        # Observability: start a tracing span for this LLM attempt.
        span = (
            self._tracer.start_span(
                f"llm.{provider}",
                ticket_id=str(ticket_id),
                correlation_id=str(correlation_id),
            )
            if self._tracer is not None
            else None
        )
        if span is not None:
            span.set_attribute("llm.provider", provider)
            span.set_attribute("llm.model", model)
        # Observability: mark the model-call start for TTFT measurement.
        if self._slo is not None:
            self._slo.record_model_start(ticket_id)

        kwargs: dict[str, Any] = {"timeout": timeout}
        if api_base:
            kwargs["api_base"] = api_base
        if api_key:
            kwargs["api_key"] = api_key

        start = time.monotonic()
        try:
            response = await self._acompletion(
                model=model,
                messages=messages,
                **kwargs,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            await self._record_attempt(
                ticket_id, run_id, provider, model,
                latency_ms, type(exc).__name__, str(exc),
                correlation_id, None, False,
            )
            if self._slo is not None:
                self._slo.record_llm_attempt(
                    provider=provider, model=model, succeeded=False,
                    fallback=(provider != "vllm"),
                )
            if span is not None:
                span.record_exception(exc)
                span.set_status(False, str(exc))
            raise FallbackTriggered(
                f"{provider} request failed: {exc}",
                type(exc).__name__,
            )

        latency_ms = int((time.monotonic() - start) * 1000)

        # Extract the content from the response.
        content = self._extract_content(response)
        if not content:
            await self._record_attempt(
                ticket_id, run_id, provider, model,
                latency_ms, "EmptyResponse", "response content was empty",
                correlation_id, None, False,
            )
            raise FallbackTriggered(f"{provider} returned empty content", "EmptyResponse")

        # Parse JSON and validate with AgentDecision.
        try:
            raw_json = self._parse_json(content)
        except (json.JSONDecodeError, ValueError) as exc:
            await self._record_attempt(
                ticket_id, run_id, provider, model,
                latency_ms, "JSONDecodeError", str(exc),
                correlation_id, None, False,
            )
            raise FallbackTriggered(
                f"{provider} returned invalid JSON: {exc}",
                "JSONDecodeError",
            )

        # Inject user_id/ticket_id into tool payload if missing (the model
        # may not populate these correctly).
        if isinstance(raw_json, dict) and raw_json.get("tool") and isinstance(raw_json["tool"], dict):
            tool_dict = raw_json["tool"]
            tool_dict.setdefault("ticket_id", str(ticket_id))
            tool_dict.setdefault("user_id", str(user_id))
            # Validate tool_name is in allow-list.
            tool_name = tool_dict.get("tool_name", "")
            if tool_name and tool_name not in self._allowed_tools:
                await self._record_attempt(
                    ticket_id, run_id, provider, model,
                    latency_ms, "InvalidToolName", f"tool '{tool_name}' not in allow-list",
                    correlation_id, None, False,
                )
                raise FallbackTriggered(
                    f"{provider} returned disallowed tool: {tool_name}",
                    "InvalidToolName",
                )

        try:
            decision = AgentDecision.model_validate(raw_json)
        except ValidationError as exc:
            await self._record_attempt(
                ticket_id, run_id, provider, model,
                latency_ms, "ValidationError", str(exc),
                correlation_id, None, False,
            )
            raise FallbackTriggered(
                f"{provider} response failed Pydantic validation: {exc}",
                "ValidationError",
            )

        # Validate action is recognized.
        if decision.action not in ("respond", "call_tool", "request_oauth"):
            await self._record_attempt(
                ticket_id, run_id, provider, model,
                latency_ms, "InvalidAction", f"action '{decision.action}' not recognized",
                correlation_id, None, False,
            )
            raise FallbackTriggered(
                f"{provider} returned unrecognized action: {decision.action}",
                "InvalidAction",
            )

        # Validate consistency: respond needs response_text, tool actions need tool.
        if decision.action == "respond" and not decision.response_text:
            await self._record_attempt(
                ticket_id, run_id, provider, model,
                latency_ms, "MissingResponseText", "respond action without response_text",
                correlation_id, None, False,
            )
            raise FallbackTriggered(
                f"{provider} respond action missing response_text",
                "MissingResponseText",
            )
        if decision.action in ("call_tool", "request_oauth") and decision.tool is None:
            await self._record_attempt(
                ticket_id, run_id, provider, model,
                latency_ms, "MissingTool", f"{decision.action} action without tool",
                correlation_id, None, False,
            )
            raise FallbackTriggered(
                f"{provider} {decision.action} action missing tool payload",
                "MissingTool",
            )

        # Success — record the attempt.
        token_usage = self._extract_usage(response)
        await self._record_attempt(
            ticket_id, run_id, provider, model,
            latency_ms, None, None,
            correlation_id, token_usage, True,
        )
        if self._slo is not None:
            self._slo.record_llm_attempt(
                provider=provider, model=model, succeeded=True,
                fallback=(provider != "vllm"),
            )
            # TTFT: for non-streaming completion, the model call latency
            # is the time to first (and only) token batch.
            self._slo.record_ttft(ticket_id)
        if span is not None:
            span.set_attribute("llm.latency_ms", latency_ms)
            span.set_status(True)

        # Stamp the selected model onto the decision.
        decision.selected_model = model
        return decision

    @staticmethod
    def _extract_content(response: Any) -> str:
        """Extract text content from a litellm completion response."""
        # litellm returns a ModelResponse with .choices[0].message.content
        if hasattr(response, "choices") and response.choices:
            msg = response.choices[0].message
            if hasattr(msg, "content"):
                return msg.content or ""
        # Fallback for dict-style responses (tests).
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
        return ""

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, Any] | None:
        """Extract token usage from a litellm completion response."""
        if hasattr(response, "usage"):
            usage = response.usage
            if hasattr(usage, "model_dump"):
                return usage.model_dump()
            if isinstance(usage, dict):
                return usage
        if isinstance(response, dict) and "usage" in response:
            return response["usage"]
        return None

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        """Parse JSON from the model output, handling markdown fences."""
        text = content.strip()
        # Strip markdown code fences if present.
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```json or ```) and last line (```).
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)
        return json.loads(text)

    async def _record_attempt(
        self,
        ticket_id: UUID,
        run_id: UUID,
        provider: str,
        model: str,
        latency_ms: int | None,
        error_class: str | None,
        error_message: str | None,
        correlation_id: UUID,
        token_usage: dict[str, Any] | None,
        succeeded: bool,
    ) -> None:
        """Record a model_attempt row if a recorder is configured."""
        if self._attempt_recorder is None:
            return
        try:
            await self._attempt_recorder.record(
                ticket_id=ticket_id,
                run_id=run_id,
                provider=provider,
                model=model,
                latency_ms=latency_ms,
                error_class=error_class,
                error_message=error_message,
                correlation_id=correlation_id,
                token_usage=token_usage,
                succeeded=succeeded,
            )
        except Exception:
            logger.warning("failed to record model_attempt", exc_info=True)


# ---------------------------------------------------------------------------
# Factory.
# ---------------------------------------------------------------------------
def build_router(
    *,
    acompletion: ACompletionProtocol | None = None,
    attempt_recorder: ModelAttemptRecorderProtocol | None = None,
) -> LLMRouter:
    """Build an ``LLMRouter`` with the default (litellm) acompletion.

    If ``acompletion`` is ``None``, lazily imports ``litellm.acompletion``.
    """
    if acompletion is None:
        try:
            import litellm

            # Cost control: enable LiteLLM's response cache so identical
            # prompts (same static prefix + same user message) return a cached
            # answer instead of re-billing the provider.  In production, swap
            # to ``type="redis"`` for a shared, distributed cache.
            if litellm.cache is None:
                litellm.cache = litellm.Cache(type="local", ttl=600)
            acompletion = litellm.acompletion  # type: ignore[assignment]
        except ImportError:
            logger.warning("litellm not installed — acompletion must be injected")
    return LLMRouter(
        acompletion=acompletion,
        attempt_recorder=attempt_recorder,
    )
