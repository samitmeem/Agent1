"""Structured logging configuration.

Configures ``structlog`` for JSON-structured logs with request/correlation
IDs throughout.  Falls back to stdlib logging when ``structlog`` is not
installed so the system works in minimal environments.

All log entries include:
- ``request_id`` / ``correlation_id`` (from context vars set by middleware).
- ``ticket_id`` (when available).
- ISO-8601 timestamps.
- Log level.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

# Context variables for request/correlation IDs.
import contextvars

# Module-level context vars — set by Gateway middleware and Worker job.
_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
_correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)
_ticket_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ticket_id", default=None
)


def set_request_id(request_id: str | None) -> None:
    """Set the current request ID in the logging context."""
    _request_id_var.set(request_id)


def set_correlation_id(correlation_id: str | None) -> None:
    """Set the current correlation ID in the logging context."""
    _correlation_id_var.set(correlation_id)


def set_ticket_id(ticket_id: str | None) -> None:
    """Set the current ticket ID in the logging context."""
    _ticket_id_var.set(ticket_id)


def get_request_id() -> str | None:
    return _request_id_var.get()


def get_correlation_id() -> str | None:
    return _correlation_id_var.get()


def get_ticket_id() -> str | None:
    return _ticket_id_var.get()


class ContextFilter(logging.Filter):
    """Inject context vars into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get() or "-"
        record.correlation_id = _correlation_id_var.get() or "-"
        record.ticket_id = _ticket_id_var.get() or "-"
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Configure structured logging.

    Uses ``structlog`` if available (JSON output); falls back to stdlib
    logging with a context-injecting filter.
    """
    try:
        import structlog  # type: ignore[import-untyped]

        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
            cache_logger_on_first_use=True,
        )
        # Also configure stdlib logging to pass through structlog.
        logging.basicConfig(
            level=level,
            stream=sys.stderr,
            format="%(message)s",
        )
        # Attach the context filter to the root logger.
        handler = logging.StreamHandler(sys.stderr)
        handler.addFilter(ContextFilter())
        logging.getLogger().addHandler(handler)
    except ImportError:
        # Fallback: stdlib logging with context injection.
        handler = logging.StreamHandler(sys.stderr)
        handler.addFilter(ContextFilter())
        formatter = logging.Formatter(
            '{"timestamp": "%(asctime)s", "level": "%(levelname)s", '
            '"logger": "%(name)s", "request_id": "%(request_id)s", '
            '"correlation_id": "%(correlation_id)s", '
            '"ticket_id": "%(ticket_id)s", "message": "%(message)s"}'
        )
        handler.setFormatter(formatter)
        root = logging.getLogger()
        root.handlers = [handler]
        root.setLevel(level)


def get_logger(name: str | None = None) -> Any:
    """Get a logger — structlog if available, else stdlib."""
    try:
        import structlog  # type: ignore[import-untyped]

        return structlog.get_logger(name)
    except ImportError:
        return logging.getLogger(name)
