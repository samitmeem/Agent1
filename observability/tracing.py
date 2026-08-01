"""Distributed tracing — OpenTelemetry with a noop fallback.

Provides a ``Tracer`` protocol that abstracts span creation so the system
works without ``opentelemetry`` installed (tests use ``NoopTracer``).
Production wiring injects a real OTel tracer via ``build_tracer``.

Spans carry ``correlation_id`` and ``ticket_id`` attributes so traces can
be correlated across Gateway → Redis → Worker → LLM → Tools.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("observability.tracing")


# ---------------------------------------------------------------------------
# Span protocol.
# ---------------------------------------------------------------------------
@runtime_checkable
class Span(Protocol):
    """A tracing span — a unit of work within a distributed trace."""

    def set_attribute(self, key: str, value: Any) -> None:
        """Attach a key-value attribute to this span."""
        ...

    def set_status(self, ok: bool, description: str | None = None) -> None:
        """Set the span status (ok/error)."""
        ...

    def record_exception(self, exc: BaseException) -> None:
        """Record an exception on this span."""
        ...

    def __enter__(self) -> Span:
        ...

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        ...


class NoopSpan:
    """No-op span — used when OpenTelemetry is not configured."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, ok: bool, description: str | None = None) -> None:
        pass

    def record_exception(self, exc: BaseException) -> None:
        pass

    def __enter__(self) -> NoopSpan:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Tracer protocol.
# ---------------------------------------------------------------------------
@runtime_checkable
class Tracer(Protocol):
    """Distributed tracer — creates spans for units of work."""

    def start_span(
        self,
        name: str,
        *,
        ticket_id: str | None = None,
        correlation_id: str | None = None,
    ) -> Span:
        """Start a new span.  ``ticket_id`` and ``correlation_id`` are
        attached as attributes for cross-service correlation."""
        ...


class NoopTracer:
    """No-op tracer — used when OpenTelemetry is not installed."""

    def start_span(
        self,
        name: str,
        *,
        ticket_id: str | None = None,
        correlation_id: str | None = None,
    ) -> Span:
        return NoopSpan()


class OTelTracer:
    """OpenTelemetry tracer wrapper.

    Lazily imports ``opentelemetry`` so the observability package loads
    without the SDK.  Spans carry ``ticket_id`` and ``correlation_id``
    attributes for cross-service trace correlation.
    """

    def __init__(self, service_name: str = "ticket-assistant") -> None:
        self._service_name = service_name
        self._tracer: Any = None

    def _ensure_tracer(self) -> Any:
        if self._tracer is not None:
            return self._tracer
        from opentelemetry import trace  # type: ignore[import-untyped]
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-untyped]
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-untyped]

        provider = TracerProvider(
            resource=Resource.create({"service.name": self._service_name})
        )
        trace.set_tracer_provider(provider)
        self._tracer = trace.get_tracer(self._service_name)
        return self._tracer

    def start_span(
        self,
        name: str,
        *,
        ticket_id: str | None = None,
        correlation_id: str | None = None,
    ) -> Span:
        tracer = self._ensure_tracer()
        otel_span = tracer.start_span(name)
        if ticket_id:
            otel_span.set_attribute("ticket.id", ticket_id)
        if correlation_id:
            otel_span.set_attribute("correlation.id", correlation_id)
        return OTelSpan(otel_span)


class OTelSpan:
    """Wraps an OpenTelemetry span to satisfy the ``Span`` protocol."""

    def __init__(self, otel_span: Any) -> None:
        self._span = otel_span

    def set_attribute(self, key: str, value: Any) -> None:
        self._span.set_attribute(key, value)

    def set_status(self, ok: bool, description: str | None = None) -> None:
        from opentelemetry.trace import Status, StatusCode  # type: ignore[import-untyped]

        status = Status(
            StatusCode.OK if ok else StatusCode.ERROR,
            description=description,
        )
        self._span.set_status(status)

    def record_exception(self, exc: BaseException) -> None:
        self._span.record_exception(exc)

    def __enter__(self) -> OTelSpan:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_val is not None:
            self.record_exception(exc_val)
            self.set_status(False, str(exc_val))


def build_tracer(service_name: str = "ticket-assistant") -> Tracer:
    """Factory for the tracer implementation.

    Attempts to load OpenTelemetry; falls back to ``NoopTracer`` if the
    SDK is not installed.  Tests inject a mock or ``NoopTracer``.
    """
    try:
        return OTelTracer(service_name=service_name)
    except ImportError:
        logger.warning("opentelemetry not installed — using NoopTracer")
        return NoopTracer()
