"""Observability package — tracing, metrics, structured logging, SLO tracking.

Public API:
- Tracing: ``Tracer``, ``Span``, ``NoopTracer``, ``build_tracer``.
- Metrics: ``MetricsRegistry``, ``NoopMetrics``, ``PrometheusMetrics``, ``build_metrics``.
- Logging: ``configure_logging``, ``get_logger``.
- SLO: ``SLOTracker`` for acceptance-target measurement.
"""
from observability.logging import (
    configure_logging,
    get_correlation_id,
    get_logger,
    get_request_id,
    get_ticket_id,
    set_correlation_id,
    set_request_id,
    set_ticket_id,
)
from observability.metrics import (
    MetricsRegistry,
    NoopMetrics,
    PrometheusMetrics,
    build_metrics,
)
from observability.slo import SLOTracker
from observability.tracing import NoopTracer, Span, Tracer, build_tracer

__all__ = [
    # Tracing
    "NoopTracer",
    "PrometheusMetrics",
    "Span",
    "Tracer",
    "build_metrics",
    "build_tracer",
    "configure_logging",
    "get_correlation_id",
    "get_logger",
    "get_request_id",
    "get_ticket_id",
    # Logging context
    "set_correlation_id",
    "set_request_id",
    "set_ticket_id",
    # Metrics
    "MetricsRegistry",
    "NoopMetrics",
    # SLO
    "SLOTracker",
]
