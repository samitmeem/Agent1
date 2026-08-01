"""SLO (Service Level Objective) tracker.

Measures the acceptance targets from `AgenticProject.md` §4:
- Text acknowledgement latency < 300 ms.
- First ``agent:status_update`` event < 1 s after message accepted.
- First ``response:chunk`` < 2 s when Qwen is available.
- Graceful text-only completion when STT/TTS unavailable.

The tracker records timestamps at each pipeline stage and reports
whether the SLO was met.  Metrics are emitted through the
``MetricsRegistry`` so they appear in Prometheus dashboards.
"""
from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from observability.metrics import MetricsRegistry, NoopMetrics

# SLO targets in milliseconds.
SLO_ACK_MS = 300
SLO_FIRST_STATUS_MS = 1000
SLO_FIRST_CHUNK_MS = 2000
SLO_TTFT_MS = 2000  # Time to first token (model start -> first token).


class SLOTracker:
    """Tracks per-ticket-run latency against acceptance SLOs.

    Lifecycle:
    1. ``record_message_accepted(ticket_id)`` — called when the Gateway
       persists a user message and dispatches ARQ.  Starts the clock.
    2. ``record_first_status(ticket_id)`` — called when the first
       ``agent:status_update`` is published.  Measures ack→status latency.
    3. ``record_first_chunk(ticket_id)`` — called when the first
       ``response:chunk`` is published.  Measures ack→chunk latency.
    4. ``record_completion(ticket_id, succeeded)`` — called on
       ``ticket:completed``.  Measures total run latency.

    Each measurement is emitted as a histogram observation and the SLO
    pass/fail is recorded as a counter.
    """

    def __init__(self, metrics: MetricsRegistry | None = None) -> None:
        self._metrics = metrics or NoopMetrics()
        # Per-ticket timestamps (monotonic).
        self._accept_times: dict[UUID, float] = {}
        self._first_status_times: dict[UUID, float] = {}
        self._first_chunk_times: dict[UUID, float] = {}
        # Per-ticket model-start timestamps for TTFT measurement.
        self._model_start_times: dict[UUID, float] = {}

    def record_message_accepted(self, ticket_id: UUID) -> None:
        """Record the moment a message is accepted (clock starts)."""
        now = time.monotonic()
        self._accept_times[ticket_id] = now

    def record_ack_latency(self, ticket_id: UUID, latency_ms: float) -> None:
        """Record the message acknowledgement latency (Gateway-side).

        This is the time from message receipt to ``message:accepted``
        being sent — measured by the Gateway WebSocket handler.
        """
        self._metrics.observe_histogram(
            "ticket_message_ack_latency_ms",
            latency_ms,
            labels={"slo_target": str(SLO_ACK_MS)},
        )
        self._metrics.inc_counter(
            "ticket_ack_slo_total",
            labels={"met": "true" if latency_ms <= SLO_ACK_MS else "false"},
        )

    def record_first_status(self, ticket_id: UUID) -> None:
        """Record the first ``agent:status_update`` event."""
        accept = self._accept_times.get(ticket_id)
        if accept is None:
            return  # No matching accept — skip.
        latency_ms = (time.monotonic() - accept) * 1000
        self._first_status_times[ticket_id] = time.monotonic()
        self._metrics.observe_histogram(
            "ticket_first_status_latency_ms",
            latency_ms,
            labels={"slo_target": str(SLO_FIRST_STATUS_MS)},
        )
        self._metrics.inc_counter(
            "ticket_first_status_slo_total",
            labels={"met": "true" if latency_ms <= SLO_FIRST_STATUS_MS else "false"},
        )

    def record_first_chunk(self, ticket_id: UUID) -> None:
        """Record the first ``response:chunk`` event."""
        accept = self._accept_times.get(ticket_id)
        if accept is None:
            return
        latency_ms = (time.monotonic() - accept) * 1000
        self._first_chunk_times[ticket_id] = time.monotonic()
        self._metrics.observe_histogram(
            "ticket_first_chunk_latency_ms",
            latency_ms,
            labels={"slo_target": str(SLO_FIRST_CHUNK_MS)},
        )
        self._metrics.inc_counter(
            "ticket_first_chunk_slo_total",
            labels={"met": "true" if latency_ms <= SLO_FIRST_CHUNK_MS else "false"},
        )

    def record_completion(
        self,
        ticket_id: UUID,
        *,
        succeeded: bool,
        voice_used: bool = False,
        voice_degraded: bool = False,
    ) -> None:
        """Record ticket completion and total run latency."""
        accept = self._accept_times.pop(ticket_id, None)
        self._first_status_times.pop(ticket_id, None)
        self._first_chunk_times.pop(ticket_id, None)

        if accept is not None:
            total_ms = (time.monotonic() - accept) * 1000
            self._metrics.observe_histogram(
                "ticket_total_run_latency_ms",
                total_ms,
            )

        self._metrics.inc_counter(
            "ticket_runs_total",
            labels={
                "status": "succeeded" if succeeded else "failed",
                "voice": "true" if voice_used else "false",
                "degraded": "true" if voice_degraded else "false",
            },
        )

    def record_model_start(self, ticket_id: UUID) -> None:
        """Record the moment the LLM model call begins (TTFT clock starts)."""
        self._model_start_times[ticket_id] = time.monotonic()

    def record_ttft(self, ticket_id: UUID) -> None:
        """Record Time to First Token — model start to first response token.

        Distinct from ``record_first_chunk`` (which measures ack→chunk):
        TTFT isolates the LLM generation latency, the acceptance target
        for streaming model responses.
        """
        start = self._model_start_times.pop(ticket_id, None)
        if start is None:
            return
        latency_ms = (time.monotonic() - start) * 1000
        self._metrics.observe_histogram(
            "llm_ttft_ms",
            latency_ms,
            labels={"slo_target": str(SLO_TTFT_MS)},
        )
        self._metrics.inc_counter(
            "llm_ttft_slo_total",
            labels={"met": "true" if latency_ms <= SLO_TTFT_MS else "false"},
        )

    def record_stt_failure(self, ticket_id: UUID) -> None:
        """Record an STT failure (graceful degradation to text)."""
        self._metrics.inc_counter(
            "stt_failures_total",
            labels={"code": "STT_UNAVAILABLE"},
        )

    def record_tts_failure(self, ticket_id: UUID) -> None:
        """Record a TTS failure (graceful degradation to text)."""
        self._metrics.inc_counter(
            "tts_failures_total",
            labels={"code": "TTS_UNAVAILABLE"},
        )

    def record_stt_latency(self, latency_ms: float, *, succeeded: bool = True) -> None:
        """Record STT transcription latency as a histogram observation."""
        self._metrics.observe_histogram(
            "stt_latency_ms",
            latency_ms,
            labels={"succeeded": "true" if succeeded else "false"},
        )
        self._metrics.inc_counter(
            "stt_transcriptions_total",
            labels={"succeeded": "true" if succeeded else "false"},
        )

    def record_tts_latency(self, latency_ms: float, *, succeeded: bool = True) -> None:
        """Record TTS synthesis latency as a histogram observation."""
        self._metrics.observe_histogram(
            "tts_latency_ms",
            latency_ms,
            labels={"succeeded": "true" if succeeded else "false"},
        )
        self._metrics.inc_counter(
            "tts_syntheses_total",
            labels={"succeeded": "true" if succeeded else "false"},
        )

    def record_llm_attempt(
        self,
        *,
        provider: str,
        model: str,
        succeeded: bool,
        fallback: bool = False,
    ) -> None:
        """Record an LLM model attempt."""
        self._metrics.inc_counter(
            "llm_attempts_total",
            labels={
                "provider": provider,
                "model": model,
                "succeeded": "true" if succeeded else "false",
                "fallback": "true" if fallback else "false",
            },
        )

    def record_tool_execution(
        self,
        *,
        tool_name: str,
        succeeded: bool,
        duration_ms: float | None = None,
    ) -> None:
        """Record a tool execution.

        When ``duration_ms`` is provided, a ``tool_execution_duration_ms``
        histogram observation is emitted alongside the counter so tool
        latency SLOs can be monitored.
        """
        self._metrics.inc_counter(
            "tool_executions_total",
            labels={
                "tool_name": tool_name,
                "succeeded": "true" if succeeded else "false",
            },
        )
        if duration_ms is not None:
            self._metrics.observe_histogram(
                "tool_execution_duration_ms",
                duration_ms,
                labels={"tool_name": tool_name},
            )
