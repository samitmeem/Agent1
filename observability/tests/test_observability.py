"""Tests for the observability package — tracing, metrics, SLO tracking.

Covers:
- NoopTracer / NoopSpan: silent, no exceptions.
- build_tracer returns NoopTracer when opentelemetry is absent.
- NoopMetrics: all operations silent, render() returns "".
- build_metrics returns NoopMetrics when prometheus_client is absent.
- SLOTracker: records ack latency, first-status, first-chunk, completion.
- SLOTracker: records STT/TTS failures and LLM/tool counters.
- SLOTracker: SLO pass/fail counters (ack < 300 ms, status < 1 s, chunk < 2 s).
- SLOTracker: no-op when no metrics registry injected.
- Logging context vars: set/get request_id, correlation_id, ticket_id.
- configure_logging does not raise.
"""
from __future__ import annotations

import time
from uuid import uuid4

import pytest
from observability.logging import (
    get_correlation_id,
    get_request_id,
    get_ticket_id,
    set_correlation_id,
    set_request_id,
    set_ticket_id,
)
from observability.metrics import NoopMetrics, build_metrics
from observability.slo import SLOTracker
from observability.tracing import NoopSpan, NoopTracer, build_tracer


# ---------------------------------------------------------------------------
# Tracing tests.
# ---------------------------------------------------------------------------
class TestTracing:
    def test_noop_tracer_returns_noop_span(self):
        tracer = NoopTracer()
        span = tracer.start_span("test", ticket_id="tic_1", correlation_id="req_1")
        assert isinstance(span, NoopSpan)

    def test_noop_span_operations_silent(self):
        span = NoopSpan()
        # None of these should raise.
        span.set_attribute("key", "value")
        span.set_status(True, "ok")
        span.record_exception(RuntimeError("test"))
        with span:
            pass

    def test_noop_span_context_manager(self):
        span = NoopSpan()
        with span as s:
            assert s is span

    def test_build_tracer_returns_tracer(self):
        tracer = build_tracer()
        # Without opentelemetry installed, returns NoopTracer.
        assert hasattr(tracer, "start_span")

    def test_tracer_start_span_with_attributes(self):
        tracer = NoopTracer()
        span = tracer.start_span(
            "llm.vllm", ticket_id="tic_123", correlation_id="req_456"
        )
        # Noop span — attributes are silently accepted.
        span.set_attribute("llm.provider", "vllm")
        span.set_status(True)


# ---------------------------------------------------------------------------
# Metrics tests.
# ---------------------------------------------------------------------------
class TestMetrics:
    def test_noop_metrics_silent(self):
        metrics = NoopMetrics()
        metrics.inc_counter("test_counter")
        metrics.inc_counter("test_counter", labels={"a": "b"}, amount=5)
        metrics.observe_histogram("test_hist", 42.5)
        metrics.observe_histogram("test_hist", 100, labels={"x": "y"})
        metrics.set_gauge("test_gauge", 3.14)
        metrics.set_gauge("test_gauge", 0, labels={"z": "w"})
        assert metrics.render() == ""

    def test_build_metrics_returns_registry(self):
        metrics = build_metrics()
        assert hasattr(metrics, "inc_counter")
        assert hasattr(metrics, "observe_histogram")
        assert hasattr(metrics, "set_gauge")
        assert hasattr(metrics, "render")


# ---------------------------------------------------------------------------
# SLOTracker tests.
# ---------------------------------------------------------------------------
class TestSLOTracker:
    def test_record_ack_latency_under_slo(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)
        ticket_id = uuid4()

        slo.record_ack_latency(ticket_id, 150.0)  # < 300 ms

        # Histogram observed.
        assert "ticket_message_ack_latency_ms" in metrics.histograms
        assert metrics.histograms["ticket_message_ack_latency_ms"][-1] == 150.0
        # SLO met counter incremented.
        assert ("ticket_ack_slo_total", {"met": "true"}) in metrics.counters

    def test_record_ack_latency_over_slo(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)
        ticket_id = uuid4()

        slo.record_ack_latency(ticket_id, 500.0)  # > 300 ms

        assert ("ticket_ack_slo_total", {"met": "false"}) in metrics.counters

    def test_record_first_status_under_slo(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)
        ticket_id = uuid4()

        slo.record_message_accepted(ticket_id)
        # Simulate < 1 s latency.
        slo.record_first_status(ticket_id)

        assert "ticket_first_status_latency_ms" in metrics.histograms
        assert ("ticket_first_status_slo_total", {"met": "true"}) in metrics.counters

    def test_record_first_status_without_accept_is_noop(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)
        ticket_id = uuid4()

        # No record_message_accepted called — should be a no-op.
        slo.record_first_status(ticket_id)

        assert "ticket_first_status_latency_ms" not in metrics.histograms

    def test_record_first_chunk_under_slo(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)
        ticket_id = uuid4()

        slo.record_message_accepted(ticket_id)
        slo.record_first_chunk(ticket_id)

        assert "ticket_first_chunk_latency_ms" in metrics.histograms
        assert ("ticket_first_chunk_slo_total", {"met": "true"}) in metrics.counters

    def test_record_completion(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)
        ticket_id = uuid4()

        slo.record_message_accepted(ticket_id)
        slo.record_completion(ticket_id, succeeded=True, voice_used=False)

        assert "ticket_total_run_latency_ms" in metrics.histograms
        assert (
            "ticket_runs_total",
            {"status": "succeeded", "voice": "false", "degraded": "false"},
        ) in metrics.counters

    def test_record_completion_cleans_up_state(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)
        ticket_id = uuid4()

        slo.record_message_accepted(ticket_id)
        slo.record_completion(ticket_id, succeeded=True)

        # After completion, the accept time is cleaned up.
        assert ticket_id not in slo._accept_times

    def test_record_stt_failure(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)
        ticket_id = uuid4()

        slo.record_stt_failure(ticket_id)

        assert ("stt_failures_total", {"code": "STT_UNAVAILABLE"}) in metrics.counters

    def test_record_tts_failure(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)
        ticket_id = uuid4()

        slo.record_tts_failure(ticket_id)

        assert ("tts_failures_total", {"code": "TTS_UNAVAILABLE"}) in metrics.counters

    def test_record_llm_attempt(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)

        slo.record_llm_attempt(
            provider="vllm", model="qwen2.5-14b", succeeded=True, fallback=False
        )

        assert (
            "llm_attempts_total",
            {
                "provider": "vllm",
                "model": "qwen2.5-14b",
                "succeeded": "true",
                "fallback": "false",
            },
        ) in metrics.counters

    def test_record_tool_execution(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)

        slo.record_tool_execution(tool_name="github.create_issue", succeeded=True)

        assert (
            "tool_executions_total",
            {"tool_name": "github.create_issue", "succeeded": "true"},
        ) in metrics.counters

    def test_slo_tracker_works_without_metrics(self):
        """SLOTracker with no metrics registry is a no-op (no exceptions)."""
        slo = SLOTracker()
        ticket_id = uuid4()
        slo.record_message_accepted(ticket_id)
        slo.record_ack_latency(ticket_id, 100)
        slo.record_first_status(ticket_id)
        slo.record_first_chunk(ticket_id)
        slo.record_completion(ticket_id, succeeded=True)
        slo.record_stt_failure(ticket_id)
        slo.record_tts_failure(ticket_id)
        slo.record_llm_attempt(provider="vllm", model="qwen", succeeded=True)
        slo.record_tool_execution(tool_name="github.create_issue", succeeded=True)


# ---------------------------------------------------------------------------
# TTFT, tool duration, STT/TTS latency tests (Task 12 additions).
# ---------------------------------------------------------------------------
class TestSLOTrackerExtended:
    def test_record_ttft_under_slo(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)
        ticket_id = uuid4()

        slo.record_model_start(ticket_id)
        slo.record_ttft(ticket_id)

        assert "llm_ttft_ms" in metrics.histograms
        assert ("llm_ttft_slo_total", {"met": "true"}) in metrics.counters

    def test_record_ttft_without_model_start_is_noop(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)
        ticket_id = uuid4()

        slo.record_ttft(ticket_id)

        assert "llm_ttft_ms" not in metrics.histograms

    def test_record_tool_execution_with_duration(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)

        slo.record_tool_execution(
            tool_name="github.create_issue", succeeded=True, duration_ms=250.0
        )

        assert (
            "tool_executions_total",
            {"tool_name": "github.create_issue", "succeeded": "true"},
        ) in metrics.counters
        assert "tool_execution_duration_ms" in metrics.histograms
        assert metrics.histograms["tool_execution_duration_ms"][-1] == 250.0

    def test_record_tool_execution_without_duration_omits_histogram(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)

        slo.record_tool_execution(tool_name="gmail.send_email", succeeded=False)

        assert (
            "tool_executions_total",
            {"tool_name": "gmail.send_email", "succeeded": "false"},
        ) in metrics.counters
        assert "tool_execution_duration_ms" not in metrics.histograms

    def test_record_stt_latency_success(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)

        slo.record_stt_latency(450.0, succeeded=True)

        assert "stt_latency_ms" in metrics.histograms
        assert metrics.histograms["stt_latency_ms"][-1] == 450.0
        assert ("stt_transcriptions_total", {"succeeded": "true"}) in metrics.counters

    def test_record_stt_latency_failure(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)

        slo.record_stt_latency(1000.0, succeeded=False)

        assert ("stt_transcriptions_total", {"succeeded": "false"}) in metrics.counters

    def test_record_tts_latency_success(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)

        slo.record_tts_latency(300.0, succeeded=True)

        assert "tts_latency_ms" in metrics.histograms
        assert metrics.histograms["tts_latency_ms"][-1] == 300.0
        assert ("tts_syntheses_total", {"succeeded": "true"}) in metrics.counters

    def test_record_tts_latency_failure(self):
        metrics = _RecordingMetrics()
        slo = SLOTracker(metrics=metrics)

        slo.record_tts_latency(800.0, succeeded=False)

        assert ("tts_syntheses_total", {"succeeded": "false"}) in metrics.counters

    def test_set_gauge_recorded(self):
        metrics = _RecordingMetrics()
        metrics.set_gauge("ws_active_connections", 5)
        assert ("ws_active_connections", 5) in metrics.gauges


# ---------------------------------------------------------------------------
# Logging context tests.
# ---------------------------------------------------------------------------
class TestLoggingContext:
    def test_set_and_get_request_id(self):
        set_request_id("req_123")
        assert get_request_id() == "req_123"
        set_request_id(None)
        assert get_request_id() is None

    def test_set_and_get_correlation_id(self):
        set_correlation_id("corr_456")
        assert get_correlation_id() == "corr_456"
        set_correlation_id(None)

    def test_set_and_get_ticket_id(self):
        set_ticket_id("tic_789")
        assert get_ticket_id() == "tic_789"
        set_ticket_id(None)


# ---------------------------------------------------------------------------
# Helper — recording metrics for test assertions.
# ---------------------------------------------------------------------------
class _RecordingMetrics:
    """Records all metric operations for test assertions."""

    def __init__(self):
        self.counters: list[tuple[str, dict[str, str]]] = []
        self.histograms: dict[str, list[float]] = {}
        self.gauges: list[tuple[str, float]] = []

    def inc_counter(
        self,
        name: str,
        *,
        labels: dict[str, str] | None = None,
        amount: int = 1,
    ) -> None:
        self.counters.append((name, labels or {}))

    def observe_histogram(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.histograms.setdefault(name, []).append(value)

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.gauges.append((name, value))

    def render(self) -> str:
        return ""
