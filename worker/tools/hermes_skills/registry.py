"""Minimal local tool registry for vendored Hermes tools.

This is a **shim** of Hermes's ``tools/registry.py`` — just enough for
vendored tool modules to self-register their schemas + handlers without
importing the full ``hermes-agent`` package or its transitive dependencies.

Design goals:
- **No external deps**: pure stdlib, so vendored tools can register even
  in a minimal environment.
- **Thread-safe**: a lock guards mutation (register) and snapshot (read).
- **Hermes-compatible API**: mirrors the subset of ``ToolRegistry`` that
  vendored tools and the adapter actually use:
    - ``register(name, toolset, schema, handler, check_fn, ...)``
    - ``_snapshot_entries()`` → list of ``ToolEntry``
    - ``dispatch(name, args, **kwargs)`` → JSON string result
- **Isolated**: this registry is a standalone singleton. It has no
  relationship to our ``worker.tools.base.ALLOWED_TOOLS`` or the
  ``ToolExecutor``. The adapter reads from here and wraps entries into
  our ``Tool`` ABC; nothing in the existing worker path reads this.

Importing this module has no side effects beyond defining the registry.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

logger = logging.getLogger("worker.tools.hermes_skills.registry")


@dataclass
class ToolEntry:
    """Metadata for a single registered Hermes tool.

    Mirrors the ``__slots__`` of Hermes's ``ToolEntry`` so vendored tool
    modules can populate the same fields.
    """

    name: str
    toolset: str
    schema: dict
    handler: Callable[..., Any]
    check_fn: Optional[Callable[[], bool]] = None
    requires_env: list[str] = field(default_factory=list)
    is_async: bool = False
    description: str = ""
    emoji: str = ""
    max_result_size_chars: Optional[int] = None


class ToolRegistry:
    """Minimal singleton registry for vendored Hermes tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._lock = threading.RLock()

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable[..., Any],
        check_fn: Optional[Callable[[], bool]] = None,
        requires_env: Optional[list[str]] = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        max_result_size_chars: Optional[int] = None,
    ) -> None:
        """Register a tool. Called at module-import time by each tool file."""
        with self._lock:
            if name in self._tools:
                logger.warning(
                    "hermes shim: tool '%s' already registered (toolset '%s') — "
                    "overwriting with toolset '%s'",
                    name,
                    self._tools[name].toolset,
                    toolset,
                )
            self._tools[name] = ToolEntry(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                check_fn=check_fn,
                requires_env=requires_env or [],
                is_async=is_async,
                description=description or schema.get("description", ""),
                emoji=emoji,
                max_result_size_chars=max_result_size_chars,
            )

    def deregister(self, name: str) -> None:
        """Remove a tool from the registry."""
        with self._lock:
            self._tools.pop(name, None)

    def get_entry(self, name: str) -> Optional[ToolEntry]:
        """Return a registered tool entry by name, or None."""
        with self._lock:
            return self._tools.get(name)

    def _snapshot_entries(self) -> List[ToolEntry]:
        """Return a stable snapshot of registered tool entries."""
        with self._lock:
            return list(self._tools.values())

    def get_all_tool_names(self) -> List[str]:
        """Return sorted list of all registered tool names."""
        return sorted(entry.name for entry in self._snapshot_entries())

    def dispatch(self, name: str, args: dict, **kwargs: Any) -> str:
        """Execute a tool handler by name.

        Mirrors Hermes's contract: returns a JSON string; catches all
        exceptions and returns ``{"error": "..."}`` on failure. Async
        handlers are **not** auto-bridged here (the adapter handles async
        detection) — this method is for sync dispatch only.
        """
        entry = self.get_entry(name)
        if entry is None:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            result = entry.handler(args, **kwargs)
            if isinstance(result, str):
                return result
            if isinstance(result, dict):
                return json.dumps(result)
            return json.dumps(
                {
                    "error": (
                        f"Tool handler returned unsupported result type: "
                        f"{type(result).__name__}"
                    )
                }
            )
        except Exception as exc:
            logger.exception("hermes shim: tool %s dispatch error: %s", name, exc)
            return json.dumps({"error": f"Tool execution failed: {exc}"})


# Module-level singleton — the only instance vendored tools register into.
registry = ToolRegistry()
