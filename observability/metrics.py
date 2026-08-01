"""Prometheus metrics with a noop fallback.

Provides a ``MetricsRegistry`` protocol that abstracts counter/histogram
operations so the system works without ``prometheus_client`` installed
(tests use ``NoopMetrics``).  Production wiring injects real Prometheus
metrics via ``build_metrics``.

Key metrics (per §4 acceptance targets):
- ``ticket_message_ack_latency_ms`` — histogram, target < 300 ms.
- ``ticket_first_status_latency_ms`` — histogram, target < 1 s.
- ``ticket_first_chunk_latency_ms`` — histogram, target < 2 s.
- ``ticket_runs_total`` — counter (by status).
- ``llm_attempts_total`` — counter (by provider, succeeded).
- ``tool_executions_total`` — counter (by tool_name, succeeded).
- ``ws_active_connections`` — gauge.
- ``stt_transcriptions_total`` / ``tts_syntheses_total`` — counters.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("observability.metrics")


# ---------------------------------------------------------------------------
# Metrics protocol.
# ---------------------------------------------------------------------------
@runtime_checkable
class MetricsRegistry(Protocol):
    """Metrics registry — counters, histograms, gauges."""

    def inc_counter(
        self,
        name: str,
        *,
        labels: dict[str, str] | None = None,
        amount: int = 1,
    ) -> None:
        """Increment a counter."""
        ...

    def observe_histogram(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Observe a value in a histogram."""
        ...

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Set a gauge value."""
        ...

    def render(self) -> str:
        """Render metrics in Prometheus text exposition format."""
        ...


# ---------------------------------------------------------------------------
# NoopMetrics — used when prometheus_client is not installed.
# ---------------------------------------------------------------------------
class NoopMetrics:
    """No-op metrics — all operations are silent."""

    def inc_counter(
        self,
        name: str,
        *,
        labels: dict[str, str] | None = None,
        amount: int = 1,
    ) -> None:
        pass

    def observe_histogram(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        pass

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        pass

    def render(self) -> str:
        return ""


# ---------------------------------------------------------------------------
# PrometheusMetrics — real implementation.
# ---------------------------------------------------------------------------
class PrometheusMetrics:
    """Prometheus-backed metrics registry.

    Lazily imports ``prometheus_client`` so the observability package
    loads without the library.  Counters, histograms, and gauges are
    created on first use and cached by name.
    """

    def __init__(self) -> None:
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._gauges: dict[str, Any] = {}
        self._label_names: dict[str, list[str]] = {}

    def _label_keys(self, labels: dict[str, str] | None) -> list[str]:
        if labels is None:
            return []
        return sorted(labels.keys())

    def _get_counter(self, name: str, label_names: list[str]) -> Any:
        from prometheus_client import Counter  # type: ignore[import-untyped]

        key = f"{name}:{':'.join(label_names)}"
        if key not in self._counters:
            self._counters[key] = Counter(
                name, f"Counter {name}", label_names
            )
        return self._counters[key]

    def _get_histogram(self, name: str, label_names: list[str]) -> Any:
        from prometheus_client import Histogram  # type: ignore[import-untyped]

        key = f"{name}:{':'.join(label_names)}"
        if key not in self._histograms:
            self._histograms[key] = Histogram(
                name,
                f"Histogram {name}",
                label_names,
                # Buckets tuned for latency SLOs (ms).
                buckets=(50, 100, 150, 200, 250, 300, 500, 750, 1000, 2000, 5000),
            )
        return self._histograms[key]

    def _get_gauge(self, name: str, label_names: list[str]) -> Any:
        from prometheus_client import Gauge  # type: ignore[import-untyped]

        key = f"{name}:{':'.join(label_names)}"
        if key not in self._gauges:
            self._gauges[key] = Gauge(name, f"Gauge {name}", label_names)
        return self._gauges[key]

    def inc_counter(
        self,
        name: str,
        *,
        labels: dict[str, str] | None = None,
        amount: int = 1,
    ) -> None:
        label_names = self._label_keys(labels)
        counter = self._get_counter(name, label_names)
        if labels:
            counter.labels(**labels).inc(amount)
        else:
            counter.inc(amount)

    def observe_histogram(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        label_names = self._label_keys(labels)
        hist = self._get_histogram(name, label_names)
        if labels:
            hist.labels(**labels).observe(value)
        else:
            hist.observe(value)

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        label_names = self._label_keys(labels)
        gauge = self._get_gauge(name, label_names)
        if labels:
            gauge.labels(**labels).set(value)
        else:
            gauge.set(value)

    def render(self) -> str:
        from prometheus_client import generate_latest  # type: ignore[import-untyped]

        return generate_latest().decode("utf-8")


def build_metrics() -> MetricsRegistry:
    """Factory for the metrics implementation.

    Attempts to load prometheus_client; falls back to ``NoopMetrics``.
    Tests inject a mock or ``NoopMetrics``.
    """
    try:
        import prometheus_client  # type: ignore[import-untyped]  # noqa: F401

        return PrometheusMetrics()
    except ImportError:
        logger.warning("prometheus_client not installed — using NoopMetrics")
        return NoopMetrics()
