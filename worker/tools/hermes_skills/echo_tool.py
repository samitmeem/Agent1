"""Vendored Hermes-style tool: ``echo`` — a read-only diagnostic tool.

This is a minimal, zero-dependency read-only tool that demonstrates the
vendored-tool pipeline end-to-end:

1. It self-registers into the local shim registry
   (``worker.tools.hermes_skills.registry``) at import time.
2. The adapter (``worker.tools.hermes_adapter``) discovers it and wraps it
   as a ``HermesToolWrapper`` (namespaced ``hermes.echo``).
3. When ``ENABLE_HERMES_TOOLS=1``, the executor admits it and the LLM
   router offers it in the system prompt.

It follows the Hermes tool contract:
- Handler signature: ``handler(args: dict, **kwargs) -> str`` (JSON string).
- ``check_fn`` gates availability (always ``True`` here — no external deps).
- ``requires_env`` is empty (no API keys needed).

This tool is **read-only** (it only echoes its input) and **non-sensitive**
(per the ``hermes_skills`` package contract — no write operations).
"""

from __future__ import annotations

import json
from typing import Any

from worker.tools.hermes_skills.registry import registry


def echo(text: str = "", **_kwargs: Any) -> str:
    """Echo the input text back as JSON.

    A trivial read-only tool useful for smoke-testing the adapter pipeline.
    """
    return json.dumps({"echoed": text, "length": len(text)})


ECHO_SCHEMA = {
    "name": "echo",
    "description": (
        "Echo the input text back. A read-only diagnostic tool for testing "
        "the Hermes tool adapter pipeline."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to echo back.",
            },
        },
        "required": ["text"],
    },
}


def _check_echo_requirements() -> bool:
    """Always available — this tool has no external dependencies."""
    return True


# Self-register into the local shim registry at module-import time.
# This mirrors the Hermes ``registry.register()`` pattern.
registry.register(
    name="echo",
    toolset="diagnostics",
    schema=ECHO_SCHEMA,
    handler=lambda args, **kw: echo(text=args.get("text", "")),
    check_fn=_check_echo_requirements,
    requires_env=[],
    is_async=False,
    description="Echo the input text back (read-only diagnostic).",
    emoji="🔊",
)
