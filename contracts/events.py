"""WebSocket event payload models and the `validate_event` dispatcher.

One payload model per event in the Core event definitions table of
`AgenticProject.md` §2. `validate_event` dispatches by the envelope `type`
field and returns the validated model, raising `ValidationError` on unknown
types or malformed payloads.
"""
from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field, ValidationError, model_validator


# ---------------------------------------------------------------------------
# Client -> Gateway
# ---------------------------------------------------------------------------
class TicketConnectPayload(BaseModel):
    """`ticket:connect` — open/resume a ticket socket with recovery."""

    last_event_id: str | None = None
    voice_enabled: bool = False


class MessageSubmitPayload(BaseModel):
    """`message:submit` — user text submission with idempotency key."""

    client_message_id: str
    text: str = Field(min_length=1)


class AudioStartPayload(BaseModel):
    """`audio:start` — begin a voice stream."""

    codec: str
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    stream_id: str


class AudioStreamPayload(BaseModel):
    """`audio:stream` — metadata preceding a binary audio frame."""

    stream_id: str
    sequence: int = Field(ge=0)


class AudioStopPayload(BaseModel):
    """`audio:stop` — end a voice stream."""

    stream_id: str


class PingPongPayload(BaseModel):
    """`ping` / `pong` — heartbeat."""

    timestamp: str


# ---------------------------------------------------------------------------
# Gateway -> Client
# ---------------------------------------------------------------------------
class TicketConnectedPayload(BaseModel):
    """`ticket:connected` — snapshot + replay boundary after connect."""

    ticket_snapshot: dict
    replay_complete: bool = True
    last_event_id: str | None = None


class MessageAcceptedPayload(BaseModel):
    """`message:accepted` — persisted message id + queue status."""

    message_id: str
    client_message_id: str
    queued: bool = True


class AudioTranscriptPartialPayload(BaseModel):
    """`audio:transcript_partial` — incremental ASR text."""

    stream_id: str
    transcript: str


class AudioTranscriptFinalPayload(BaseModel):
    """`audio:transcript_final` — final transcript, persisted as a message."""

    stream_id: str
    transcript: str
    message_id: str


class AgentStatusUpdatePayload(BaseModel):
    """`agent:status_update` — worker state change surfaced to the client."""

    status: str
    label: str | None = None
    tool_name: str | None = None
    detail: str | None = None


class ResponseChunkPayload(BaseModel):
    """`response:chunk` — ordered text delta."""

    sequence: int = Field(ge=0)
    delta: str


class ResponseAudioStartPayload(BaseModel):
    """`response:audio_start` — precedes binary TTS frames."""

    stream_id: str
    mime_type: str
    sample_rate: int = Field(gt=0)


class ResponseAudioEndPayload(BaseModel):
    """`response:audio_end` — TTS stream complete."""

    stream_id: str


class ResponseFinalPayload(BaseModel):
    """`response:final` — persisted assistant message + citations/actions."""

    message_id: str
    text: str
    citations: list[dict] = Field(default_factory=list)
    actions: list[dict] = Field(default_factory=list)


class TicketCompletedPayload(BaseModel):
    """`ticket:completed` — final ticket status + model/fallback reason."""

    status: str
    selected_model: str | None = None
    fallback_reason: str | None = None


class TicketErrorPayload(BaseModel):
    """`ticket:error` — safe, retryable error surfaced to the client."""

    code: str
    message: str
    retryable: bool = False
    correlation_id: str | None = None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
EventPayload = Union[
    AudioStartPayload,
    AudioStopPayload,
    AudioStreamPayload,
    AudioTranscriptFinalPayload,
    AudioTranscriptPartialPayload,
    AgentStatusUpdatePayload,
    MessageAcceptedPayload,
    MessageSubmitPayload,
    PingPongPayload,
    ResponseAudioEndPayload,
    ResponseAudioStartPayload,
    ResponseChunkPayload,
    ResponseFinalPayload,
    TicketCompletedPayload,
    TicketConnectPayload,
    TicketConnectedPayload,
    TicketErrorPayload,
]

_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "ticket:connect": TicketConnectPayload,
    "ticket:connected": TicketConnectedPayload,
    "message:submit": MessageSubmitPayload,
    "message:accepted": MessageAcceptedPayload,
    "audio:start": AudioStartPayload,
    "audio:stream": AudioStreamPayload,
    "audio:stop": AudioStopPayload,
    "audio:transcript_partial": AudioTranscriptPartialPayload,
    "audio:transcript_final": AudioTranscriptFinalPayload,
    "agent:status_update": AgentStatusUpdatePayload,
    "response:chunk": ResponseChunkPayload,
    "response:audio_start": ResponseAudioStartPayload,
    "response:audio_end": ResponseAudioEndPayload,
    "response:final": ResponseFinalPayload,
    "ticket:completed": TicketCompletedPayload,
    "ticket:error": TicketErrorPayload,
    "ping": PingPongPayload,
    "pong": PingPongPayload,
}


def validate_event(type: str, payload: dict) -> BaseModel:
    """Dispatch by event `type` and validate `payload`.

    Returns the validated payload model. Raises `ValidationError` for unknown
    event types or malformed payloads.
    """
    model_cls = _EVENT_MODELS.get(type)
    if model_cls is None:
        raise ValidationError(
            f"Unknown event type: {type!r}",  # type: ignore[arg-type]
            model=BaseModel,
        )
    return model_cls.model_validate(payload)
