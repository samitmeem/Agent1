"""Isolated package for vendored Hermes Agent tools.

This package is the **only** place Hermes tool modules live. It is imported
exclusively by ``worker.tools.hermes_adapter.discover_hermes_tools()`` and is
gated behind the ``ENABLE_HERMES_TOOLS=1`` feature flag. When the flag is
unset (the default), this package is never imported and has zero effect on
the worker.

Isolation contract
-------------------
1. No module in this package may import from ``worker.tools.base``,
   ``worker.tools.executor``, ``worker.tools.github``, or
   ``worker.tools.gmail``. Hermes tools are self-contained.
2. Tools self-register into the local ``registry`` shim
   (``worker.tools.hermes_skills.registry``), not into any global singleton.
3. Each tool must provide a ``check_fn`` that returns ``False`` when its
   dependencies (pip packages, env vars, system binaries) are missing, so
   unavailable tools are silently excluded rather than crashing the worker.
4. Only **read-only / non-sensitive** tools belong here. Write operations
   are excluded until a follow-up phase wires them into
   ``SENSITIVE_TOOLS`` + ``UserConfirmationProtocol``.

Auto-discovery
--------------
Each tool module is imported here so its ``registry.register()`` call runs
at package-import time. The adapter (``worker.tools.hermes_adapter``)
imports this package, then reads the shim registry to collect all
registered tools. To add a new vendored tool, import it below.
"""

# Importing tool modules triggers their ``registry.register()`` calls.
# Each import is guarded so a broken tool never blocks the others.
try:
    from . import echo_tool  # noqa: F401 — side-effect: self-registers
except Exception:  # pragma: no cover — defensive isolation
    pass
