Below is the production implementation specification for the ticket-assistant architecture.

## 1. End-to-End System Sequence

### Ownership of state

| Component | Responsibility | State type |
|---|---|---|
| Vite/React client | UI, WebSocket lifecycle, microphone capture/playback | Ephemeral client state |
| FastAPI Gateway | Authentication, ticket authorization, WebSocket fan-out, persistence orchestration | Stateless except active socket registry |
| PostgreSQL | Users, tickets, messages, tool executions, OAuth connections, audit trail | Durable source of truth |
| Redis + ARQ | Job dispatch, distributed locks, realtime event fan-out, short-lived session data | Ephemeral |
| LangGraph worker | Per-ticket reasoning and tool-execution state machine | Checkpointed state |
| LiteLLM | Local-Qwen-first model routing and DeepSeek fallback | Stateless |
| Voice service | Faster-Whisper transcription and Kokoro speech synthesis | Low-latency session processing |

### Ticket lifecycle

1. **Client starts**
   - React loads the authenticated user and active/recent tickets through `GET /api/tickets`.
   - The client opens a WebSocket only when a ticket is viewed or created.
   - The JWT is sent through a secure cookie or `Sec-WebSocket-Protocol`; do not place access tokens in query parameters.

2. **Ticket is created or resumed**
   - New ticket: `POST /api/tickets` creates a PostgreSQL `tickets` row with `status="open"`.
   - Resumed ticket: the client connects to `WS /ws/tickets/{ticket_id}` and sends `ticket:connect`.
   - Gateway verifies the JWT and ticket ownership before accepting the connection.

3. **WebSocket recovery**
   - Client includes `last_event_id` in `ticket:connect`.
   - Gateway replays durable events/messages from PostgreSQL that occurred after that event ID.
   - The client resumes live events once replay is complete.

4. **Text input**
   - Client sends a `message:submit` event with a UUID `client_message_id`.
   - Gateway validates schema, checks ticket ownership, and inserts the user message in PostgreSQL using a unique constraint on `(ticket_id, client_message_id)`.
   - Gateway emits `message:accepted`, persists an outbox event, and enqueues a single ARQ job using an idempotency key such as `{ticket_id}:{message_id}`.

5. **Voice input**
   - Client sends `audio:start`, then 250–500 ms audio frames, then `audio:stop`.
   - Audio frames must not be placed directly in ARQ. They are latency-sensitive and can overwhelm Redis.
   - Gateway forwards frames to the voice/STT session, which performs VAD and incremental transcription.
   - Partial transcript events are sent as `audio:transcript_partial`.
   - When the final transcript arrives, Gateway stores it as a normal user message and follows the same ARQ workflow as text input.

6. **ARQ worker starts the ticket run**
   - Worker acquires Redis lock `lock:ticket:{ticket_id}` with a lease longer than the expected run duration.
   - If the ticket is already running, the worker exits safely; the existing run owns processing.
   - Worker loads ticket history, OAuth connection metadata, and LangGraph checkpoint state from PostgreSQL.

7. **LangGraph processes the ticket**
   - Suggested graph:

```text
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
```

   - Every graph invocation is isolated by `ticket_id`.
   - Persist checkpoints after tool selection, before tool execution, and after final response generation.

8. **LiteLLM routes the model request**
   - First attempt: local `Qwen 2.5 14B Instruct` through vLLM.
   - Validate the returned structured response and tool-call arguments with Pydantic before using it.
   - On timeout, malformed JSON, unsupported tool-call shape, or validation failure, retry through `deepseek/deepseek-chat`.
   - Never execute a tool until a validated tool payload is persisted with an idempotency key.

9. **Tool execution**
   - LangGraph publishes `agent:status_update` such as `planning`, `calling_tool`, or `awaiting_authorization`.
   - For Gmail/GitHub, the worker retrieves encrypted OAuth credentials, refreshes tokens if needed, and invokes the connector through `httpx`.
   - Tool execution records are stored before and after invocation. A retry reuses the same tool-execution ID, preventing duplicate emails, issues, or comments.

10. **Streaming response delivery**
    - Worker publishes chunk/status events to Redis Pub/Sub or Redis Streams under `ticket:{ticket_id}:events`.
    - Gateway subscribes once per active ticket and fans events to all authorized browser tabs.
    - Text chunks are sent as `response:chunk`; final content is persisted as an assistant message before sending `response:final`.

11. **Voice response delivery**
    - For voice-enabled sessions, buffer text by sentence or punctuation boundary.
    - Send each sentence to Kokoro TTS and deliver audio frames as binary WebSocket frames, preceded by a `response:audio_start` envelope.
    - The client queues audio frames in order, while continuing to render text chunks.

12. **Completion**
    - Worker persists final response, usage metadata, selected model/fallback reason, and ticket status.
    - Worker emits `ticket:completed`, releases its Redis lock, and acknowledges the ARQ job.
    - The ticket can remain `open` for follow-up or transition to `resolved` when the graph’s completion criteria are satisfied.

---

## 2. Technical Contracts & Schemas

### WebSocket envelope

All JSON frames use one envelope. Binary audio frames use a preceding `audio:stream` metadata event.

```json
{
  "event_id": "evt_01JQ...",
  "type": "message:submit",
  "ticket_id": "tic_01JQ...",
  "sequence": 42,
  "correlation_id": "req_01JQ...",
  "payload": {},
  "sent_at": "2026-07-28T09:30:00Z"
}
```

`event_id` is server-generated for outbound events. `correlation_id` traces one client action through Gateway, Redis, worker, model, and tools.

### Core event definitions

| Event | Direction | Payload |
|---|---|---|
| `ticket:connect` | Client → Gateway | `last_event_id`, `voice_enabled` |
| `ticket:connected` | Gateway → Client | ticket snapshot, replay boundary |
| `message:submit` | Client → Gateway | `client_message_id`, `text` |
| `message:accepted` | Gateway → Client | persisted `message_id`, queue status |
| `audio:start` | Client → Gateway | codec, sample rate, channels, stream ID |
| `audio:stream` | Client → Gateway | metadata, immediately followed by binary bytes |
| `audio:stop` | Client → Gateway | stream ID |
| `audio:transcript_partial` | Gateway → Client | temporary transcript |
| `audio:transcript_final` | Gateway → Client | final transcript and message ID |
| `agent:status_update` | Worker → Client | current state and user-facing detail |
| `response:chunk` | Worker → Client | ordered text delta |
| `response:audio_start` | Worker → Client | stream ID, MIME type, sample rate |
| `response:audio_end` | Worker → Client | stream completion |
| `response:final` | Worker → Client | final message ID, full text, citations/actions |
| `ticket:completed` | Worker → Client | final ticket status |
| `ticket:error` | Gateway/Worker → Client | safe error code, retryability, correlation ID |
| `ping` / `pong` | Both directions | heartbeat timestamp |

Example client message:

```json
{
  "type": "message:submit",
  "ticket_id": "tic_01JQ8PKJ",
  "correlation_id": "req_01JQ8Q21",
  "payload": {
    "client_message_id": "4c3904a5-7f2c-4b85-a45f-fa0a03be84ca",
    "text": "Create a GitHub issue for the production login failure."
  }
}
```

Example status event:

```json
{
  "event_id": "evt_01JQ8R0P",
  "type": "agent:status_update",
  "ticket_id": "tic_01JQ8PKJ",
  "sequence": 17,
  "payload": {
    "status": "calling_tool",
    "label": "Creating GitHub issue",
    "tool_name": "github.create_issue"
  }
}
```

### Pydantic models

```python
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator


class TicketStatus(StrEnum):
    OPEN = "open"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    RESOLVED = "resolved"
    FAILED = "failed"


class AgentStatus(StrEnum):
    IDLE = "idle"
    PLANNING = "planning"
    GENERATING = "generating"
    CALLING_TOOL = "calling_tool"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    SYNTHESIZING_VOICE = "synthesizing_voice"
    COMPLETED = "completed"


class TicketState(BaseModel):
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
    execution_id: UUID
    ticket_id: UUID
    user_id: UUID
    tool_name: Literal[
        "github.create_issue",
        "github.comment_issue",
        "gmail.send_email",
        "gmail.search_messages",
    ]
    idempotency_key: str = Field(min_length=16, max_length=128)
    arguments: dict[str, Any]
    oauth_connection_id: UUID | None = None
    dry_run: bool = False

    @field_validator("arguments")
    @classmethod
    def require_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("Tool arguments cannot be empty")
        return value


class GitHubCreateIssueArgs(BaseModel):
    repository: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1)
    labels: list[str] = Field(default_factory=list)


class GatewayEvent(BaseModel):
    type: str
    ticket_id: UUID
    correlation_id: UUID
    payload: dict[str, Any]
```

### Validated router contract

```python
import os
from litellm import acompletion
from pydantic import BaseModel, ValidationError


class AgentDecision(BaseModel):
    action: str  # "respond" | "call_tool" | "request_oauth"
    response_text: str | None = None
    tool: ToolPayload | None = None


class LLMRouter:
    async def decide(self, messages: list[dict], ticket_id: str) -> AgentDecision:
        providers = [
            {
                "model": "openai/qwen-2.5-14b",
                "api_base": os.environ["VLLM_BASE_URL"],
                "api_key": "local",
                "timeout": 12,
            },
            {
                "model": "deepseek/deepseek-chat",
                "api_key": os.environ["DEEPSEEK_API_KEY"],
                "timeout": 25,
            },
        ]

        errors: list[str] = []
        for provider in providers:
            try:
                result = await acompletion(
                    **provider,
                    messages=messages,
                    response_format=AgentDecision,
                )
                raw_json = result.choices[0].message.content
                return AgentDecision.model_validate_json(raw_json)
            except (ValidationError, ValueError, TimeoutError) as exc:
                errors.append(f"{provider['model']}: {type(exc).__name__}")

        raise RuntimeError(f"No validated LLM decision: {'; '.join(errors)}")
```

Use `acompletion`, not synchronous `completion`, inside async FastAPI and ARQ code.

### Gateway lifespan and connection management

```python
from contextlib import asynccontextmanager
from collections import defaultdict

import asyncpg
from fastapi import FastAPI, WebSocket
from redis.asyncio import Redis


class TicketConnections:
    def __init__(self) -> None:
        self._sockets: dict[str, set[WebSocket]] = defaultdict(set)

    async def add(self, ticket_id: str, ws: WebSocket) -> None:
        self._sockets[ticket_id].add(ws)

    def remove(self, ticket_id: str, ws: WebSocket) -> None:
        self._sockets[ticket_id].discard(ws)
        if not self._sockets[ticket_id]:
            self._sockets.pop(ticket_id, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pg = await asyncpg.create_pool(
        dsn="postgresql://...",
        min_size=5,
        max_size=25,
        command_timeout=15,
    )
    app.state.redis = Redis.from_url("redis://redis:6379/0", decode_responses=False)
    app.state.connections = TicketConnections()
    yield
    await app.state.redis.aclose()
    await app.state.pg.close()


app = FastAPI(lifespan=lifespan)
```

---

## 3. Robustness & Edge Cases

### Local Qwen failure and DeepSeek fallback

Fallback must occur when any of these conditions occurs:

- vLLM connection, timeout, or overload failure.
- Empty answer or incomplete streaming response.
- Invalid JSON or JSON that fails Pydantic validation.
- Tool name is not in the allow-list.
- Tool arguments are invalid.
- Local model attempts an unsafe/unrecognized action.

Fallback rules:

1. Record `model_attempt` with provider, latency, error class, and correlation ID.
2. Do not execute an unvalidated local tool call.
3. Retry only the model decision through DeepSeek.
4. Keep the same `execution_id` and idempotency key if the action is approved.
5. If DeepSeek also fails, set ticket state to `failed`, persist a safe error, and allow manual retry.

### Duplicate and out-of-order messages

- Require a `client_message_id` for every user submission.
- Add a database unique index on `(ticket_id, client_message_id)`.
- Return the existing persisted message for a duplicate request.
- Use monotonically increasing ticket event `sequence` values.
- Client discards duplicate or older outbound events but requests replay if it detects a sequence gap.

### WebSocket reconnect policy

- Heartbeat every 20 seconds; consider the socket dead after two missed heartbeats.
- Reconnect with exponential backoff plus jitter: 1s, 2s, 4s, 8s, then maximum 20s.
- On reconnect, send `ticket:connect` with the most recent durable `event_id`.
- Never assume an in-memory socket is authoritative; PostgreSQL remains the replay source.
- Limit active connections per user/ticket, for example five tabs, and close the oldest connection if exceeded.

### Redis, ARQ, and worker resiliency

- Use ARQ retries only for transient failures: Redis outage, provider timeout, or temporary upstream 5xx.
- Use a distributed ticket lock to prevent parallel execution of the same ticket.
- Renew locks for long tool/model runs; abort safely if lock ownership is lost.
- Use a transactional outbox: persist message + `ticket.run_requested` in PostgreSQL together, then dispatch to ARQ. A sweeper republishes undelivered outbox rows.
- Store LangGraph checkpoints in PostgreSQL; do not rely solely on Redis job memory.

### Tool and OAuth safety

- Encrypt OAuth refresh tokens at rest using a KMS-managed key.
- Store only connection IDs and provider metadata in LangGraph state.
- Refresh OAuth tokens under a per-connection lock.
- Require user confirmation for externally visible actions such as sending email, unless an explicit approved automation policy exists.
- Tool calls must be idempotent, logged, allow-listed, rate-limited, and timeout-bounded.

### Voice-specific handling

- Standardize initial browser upload to `audio/webm;codecs=opus`, then decode server-side.
- Apply VAD before transcription to reduce unnecessary model work.
- Preserve final transcript plus optional audio retention metadata; do not retain raw voice indefinitely by default.
- If STT fails, emit `ticket:error` with `code="STT_UNAVAILABLE"` and offer text input without losing the ticket context.
- If TTS fails, complete the ticket with text; voice is an enhancement, not a completion dependency.

---

## 4. Developer Task Breakdown

### Frontend — Vite, React, TypeScript

- Create authenticated ticket dashboard, ticket list, conversation view, status badge, and tool-confirmation UI.
- Implement `useTicketSocket(ticketId)`:
  - heartbeat;
  - reconnect/backoff;
  - event sequence handling;
  - replay cursor persistence;
  - JSON and binary-frame support.
- Implement `useAudioRecorder()` with permission handling, codec detection, chunked recording, pause/resume, and clean teardown.
- Render partial ASR text separately from persisted final messages.
- Stream response chunks into a temporary assistant bubble, then replace with `response:final`.
- Create ordered TTS audio queue with mute/stop controls.
- Add tests for reconnection, duplicate events, audio permission denial, and interrupted recording.

### Gateway — FastAPI, PostgreSQL, Redis

- Build JWT authentication and ticket-level authorization.
- Create REST endpoints:
  - `POST /api/tickets`
  - `GET /api/tickets`
  - `GET /api/tickets/{ticket_id}`
  - `POST /api/oauth/{provider}/connect`
  - `GET /api/oauth/{provider}/callback`
- Build WebSocket endpoint and validate every event using Pydantic.
- Implement database transaction for message persistence, event sequence allocation, outbox insert, and ARQ dispatch.
- Add Redis Pub/Sub or Streams bridge from worker events to WebSocket clients.
- Configure shared asyncpg and Redis pools through FastAPI lifespan.
- Add request IDs, correlation IDs, structured logs, rate limits, and safe error responses.
- Write Alembic migrations for users, tickets, messages, events, tool executions, OAuth connections, and outbox events.

### Worker — ARQ and LangGraph

- Define the LangGraph graph and its explicit transitions.
- Add PostgreSQL checkpoints keyed by `(ticket_id, run_id)`.
- Implement `run_ticket(ticket_id, message_id)` ARQ job with distributed lock and idempotency.
- Load only the ticket’s authorized conversation/context; never mix data across tickets.
- Publish status, chunks, tool confirmation requests, and final events through Redis.
- Persist every tool attempt/result, model attempt, final answer, and completion state.
- Add retry classification: transient, permanent, authorization-needed, and user-input-needed.
- Build integration tests for concurrent inputs, worker crash/restart, lock expiry, and interrupted tool execution.

### LLM, tools, and voice services

- Configure LiteLLM with Qwen/vLLM primary and DeepSeek fallback.
- Create strict Pydantic response schemas and allow-listed tool definitions.
- Log model selection, fallback reason, latency, token usage, and validation outcomes.
- Implement OAuth token refresh, provider scopes, connector timeouts, and idempotent external tool requests.
- Implement Faster-Whisper session processing with VAD and partial/final transcript events.
- Implement Kokoro sentence-level synthesis and audio frame streaming.
- Add health checks for vLLM, DeepSeek connectivity, STT, TTS, Redis, and PostgreSQL.
- Establish acceptance targets:
  - text acknowledgement under 300 ms;
  - first agent status event under 1 second;
  - initial text response under 2 seconds when local Qwen is available;
  - graceful text-only completion when STT/TTS is unavailable.