"""Zero-risk adapter: wraps Hermes-registry tools as our ``Tool`` subclasses.

This module is **optional**. It is never imported by
``worker/tools/__init__.py``, ``worker/tools/executor.py``, or
``worker/graph.py`` unless explicitly enabled via the
``ENABLE_HERMES_TOOLS=1`` feature flag (see ``worker/entrypoint.py``).

Importing this module has no side effects on the existing 4 tools
(``github.*``, ``gmail.*``). It only defines the wrapper class and the
discovery function.

Architecture
------------
1. ``discover_hermes_tools()`` imports the ``worker.tools.hermes_skills``
   package (which triggers each vendored tool's ``registry.register()``
   call) and reads the local shim registry.
2. For each registered Hermes tool, it builds a ``HermesToolWrapper``
   instance — a subclass of our ``Tool`` ABC.
3. The wrapper's ``_invoke`` calls the Hermes handler (sync or async),
   parses the JSON-string result, and maps it to our ``ToolResult``
   dataclass.
4. Hermes tools are namespaced ``hermes.<name>`` to avoid collisions with
   ``github.*`` / ``gmail.*``.

Safety properties
-----------------
- ``discover_hermes_tools()`` never raises — a broken or missing Hermes
  package returns ``[]`` and the worker boots normally with just the
  existing 4 tools.
- Hermes tools use env vars (not our OAuth store), so the ``credential``
  argument is ignored by the wrapper.
- The wrapper does **not** participate in ``tool_executions`` idempotency
  or ``UserConfirmationProtocol`` — only read-only / non-sensitive Hermes
  tools are vendored (enforced by the ``hermes_skills`` package contract).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from contracts.models import ToolPayload
from worker.tools.base import OAuthCredential, Tool, ToolResult

logger = logging.getLogger("worker.tools.hermes_adapter")


class HermesToolWrapper(Tool):
    """Adapts a Hermes registry handler into our ``Tool`` ABC.

    - ``name``: namespaced as ``hermes.<original_name>`` to avoid
      collisions with our ``github.*`` / ``gmail.*`` tools.
    - ``_invoke``: calls the Hermes handler, parses the JSON-string
      result, and maps it to ``ToolResult(success=..., data=..., error=...)``.
    - OAuth: Hermes tools use env vars, not our OAuth store — the
      ``credential`` argument is accepted but ignored.
    """

    def __init__(
        self,
        hermes_name: str,
        handler: Any,
        schema: dict,
        *,
        is_async: bool = False,
    ) -> None:
        # Don't create an httpx client — Hermes tools manage their own HTTP.
        # We pass a dummy client to satisfy the base __init__ but never use it.
        super().__init__(http_client=None)
        self.name = f"hermes.{hermes_name}"
        self._hermes_name = hermes_name
        self._handler = handler
        self._schema = schema
        self._is_async = is_async

    async def _invoke(
        self,
        payload: ToolPayload,
        credential: OAuthCredential | None,  # noqa: ARG002 — unused by Hermes tools
    ) -> ToolResult:
        """Call the Hermes handler and map the result to ``ToolResult``."""
        try:
            result = self._handler(
                payload.arguments,
                task_id=str(payload.ticket_id),
            )
            if self._is_async or asyncio.iscoroutine(result):
                result = await result  # type: ignore[assignment]
        except Exception as exc:
            logger.exception("hermes tool %s raised: %s", self.name, exc)
            return ToolResult(
                success=False,
                tool_name=self.name,
                error=f"hermes tool error: {exc}",
            )

        # Hermes contract: result is a JSON string (or a dict).
        parsed = self._parse_result(result)
        if parsed is None:
            return ToolResult(
                success=False,
                tool_name=self.name,
                error=f"hermes handler returned non-JSON: {str(result)[:200]}",
            )

        if isinstance(parsed, dict) and "error" in parsed:
            return ToolResult(
                success=False,
                tool_name=self.name,
                error=str(parsed["error"]),
                data=parsed,
            )

        return ToolResult(
            success=True,
            tool_name=self.name,
            data=parsed if isinstance(parsed, dict) else {"result": parsed},
        )

    @staticmethod
    def _parse_result(result: Any) -> Any:
        """Parse a Hermes handler result into a Python object.

        Returns ``None`` if the result cannot be parsed as JSON.
        """
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                return json.loads(result)
            except (json.JSONDecodeError, ValueError):
                return None
        # Non-string, non-dict results are not part of the Hermes contract.
        return None


def discover_hermes_tools() -> list[HermesToolWrapper]:
    """Import the ``hermes_skills`` package and collect registered tools.

    Returns ``[]`` if the package is empty, missing, or fails to import.
    **Never raises** — a broken Hermes tool must not crash the worker.

    Each tool's ``check_fn`` is **not** called here; the adapter trusts the
    vendored package to only register tools whose ``check_fn`` would pass.
    Runtime availability gating happens at the LLM-router schema level
    (tools not in the allow-list are never offered to the model).
    """
    wrappers: list[HermesToolWrapper] = []
    try:
        # Importing the package triggers each tool module's
        # ``registry.register()`` calls at module level.
        import worker.tools.hermes_skills  # noqa: F401 — side-effect import
        from worker.tools.hermes_skills.registry import registry
    except Exception:
        logger.debug(
            "hermes_skills package unavailable — no Hermes tools exposed",
            exc_info=True,
        )
        return []

    for entry in registry._snapshot_entries():
        try:
            wrappers.append(
                HermesToolWrapper(
                    hermes_name=entry.name,
                    handler=entry.handler,
                    schema=entry.schema,
                    is_async=entry.is_async,
                )
            )
        except Exception:
            logger.warning(
                "failed to wrap hermes tool '%s' — skipping",
                entry.name,
                exc_info=True,
            )
    return wrappers
