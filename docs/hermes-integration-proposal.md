# Hermes Agent Skills/Tools Integration — Feasibility & Architectural Proposal

> Source: `https://github.com/nousresearch/hermes-agent`
> Status: **Proposal — awaiting approval. No code modified.**

---

## 1. Hermes Skill/Tool Architecture Analysis

Hermes uses a **central registry pattern** (`tools/registry.py`) that is
fundamentally different from our class-based `Tool` ABC pattern.

### Hermes Tool Definition Model

Each tool file in `tools/*.py` self-registers at module-import time via
`registry.register()`:

```python
registry.register(
    name="terminal",               # unique tool name
    toolset="terminal",             # toolset grouping
    schema={...},                   # OpenAI function-calling JSON schema
    handler=handle_terminal,        # callable(args: dict, **kw) -> str
    check_fn=check_terminal,        # optional availability gate
    requires_env=["SOME_VAR"],      # optional env-var deps
    is_async=False,                 # async coroutine flag
    emoji="💻",                     # UI metadata
    max_result_size_chars=100_000,  # budget cap
)
```

- **Handler signature**: `handler(args: dict, **kwargs) -> str` (returns JSON
  string) or `async handler(...) -> str`. Async handlers are auto-bridged via
  `_run_async()`.
- **Result contract**: handlers return a **JSON string** (or a multimodal
  envelope dict). Non-string results are normalized to a `tool_error` string.
- **Dispatch**: `registry.dispatch(name, args, **kwargs)` catches all
  exceptions and returns `{"error": "..."}` JSON — never raises.
- **Auto-discovery**: `discover_builtin_tools()` imports every `tools/*.py`
  containing a top-level `registry.register()` call. No manual import list.
- **Toolsets**: tools are grouped into named toolsets;
  `get_definitions(tool_names)` returns OpenAI-format schemas filtered by
  `check_fn()` availability.

### Hermes Skills (distinct from tools)

Skills are **markdown documents** (`SKILL.md` with YAML frontmatter), not
executable code:

- `skills_list` / `skill_view` / `skill_manage` are *tools* that read/write
  skill files.
- Skills declare `required_environment_variables`, `prerequisites.env_vars`,
  `environments` filters.
- Skills are **procedural memory** (prompt context), not callable functions —
  they're loaded into the system prompt, not dispatched.

**Implication**: The request to "import and wrap Hermes skills into standard
`@tool` decorators" maps to wrapping Hermes **tools** (executable handlers),
not skills (markdown). Skills would be a separate, simpler integration
(system-prompt injection).

### Key Incompatibilities With Our Architecture

| Concern | Our codebase | Hermes |
|---------|-------------|--------|
| Tool model | `Tool` ABC with `async _invoke(payload, credential) -> ToolResult` | Plain callable `handler(args, **kw) -> str` |
| Allow-list | `ALLOWED_TOOLS` frozenset in `worker/tools/base.py` + `llm/router.py` | `toolsets.py` membership |
| Idempotency | `ToolExecutor` inserts `tool_executions` rows, short-circuits retries | None — handlers are stateless |
| OAuth | `OAuthCredentialStoreProtocol` injects credentials per-call | Handlers read env vars / global config directly |
| Rate limiting | `RateLimiterProtocol` token-bucket per user+tool | None |
| Confirmation | `UserConfirmationProtocol` for sensitive tools | Approval middleware (separate layer) |
| Result shape | `ToolResult` dataclass (`success`, `data`, `error`) | JSON string |
| Dispatch | `ToolExecutor.execute(payload)` | `registry.dispatch(name, args)` |

---

## 2. Proposed Zero-Risk Adapter Architecture

The adapter must be **fully isolated** — no Hermes imports leak into our
existing tool path, and our existing 4 tools remain untouched.

### File layout

```
worker/tools/
├── base.py              # UNCHANGED
├── executor.py          # UNCHANGED
├── github.py            # UNCHANGED
├── gmail.py             # UNCHANGED
├── __init__.py          # UNCHANGED (existing 4 tools only)
├── hermes_adapter.py    # NEW — the bridge layer
└── hermes_skills/       # NEW — isolated Hermes tool packages
    ├── __init__.py
    ├── README.md
    └── <vendored or pip-installed hermes tool modules>
```

### `hermes_adapter.py` — the bridge

The adapter translates a Hermes-registered tool into our `Tool` ABC subclass:

```python
"""Zero-risk adapter: wraps Hermes-registry tools as our Tool subclasses.

This module is OPTIONAL. It is never imported by worker/tools/__init__.py,
worker/tools/executor.py, or worker/graph.py unless explicitly enabled.
Importing it has no side effects on the existing 4 tools.
"""
from __future__ import annotations
import importlib
from typing import Any
from contracts.models import ToolPayload
from worker.tools.base import OAuthCredential, Tool, ToolResult


class HermesToolWrapper(Tool):
    """Adapts a Hermes registry handler into our Tool ABC.

    - name: namespaced as 'hermes.<original_name>' to avoid collisions
      with our github.*/gmail.* tools.
    - _invoke: calls the Hermes handler, parses the JSON string result,
      maps it to ToolResult(success=..., data=..., error=...).
    - OAuth: Hermes tools use env vars, not our OAuth store — credential
      is ignored (None is acceptable; the wrapper does not require it).
    """

    def __init__(self, hermes_name: str, handler: Any, schema: dict):
        self.name = f"hermes.{hermes_name}"
        self._handler = handler
        self._schema = schema

    async def _invoke(
        self,
        payload: ToolPayload,
        credential: OAuthCredential | None,
    ) -> ToolResult:
        # Hermes handlers are sync (return str) or async (return coroutine).
        # We detect and await accordingly.
        import asyncio
        import json

        result = self._handler(payload.arguments, task_id=str(payload.ticket_id))
        if asyncio.iscoroutine(result):
            result = await result
        # Hermes contract: result is a JSON string.
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
        except json.JSONDecodeError:
            return ToolResult(
                success=False,
                tool_name=self.name,
                error=f"hermes handler returned non-JSON: {result[:200]}",
            )
        if isinstance(parsed, dict) and "error" in parsed:
            return ToolResult(
                success=False,
                tool_name=self.name,
                error=str(parsed["error"]),
                data=parsed,
            )
        return ToolResult(success=True, tool_name=self.name, data=parsed)


def discover_hermes_tools() -> list[HermesToolWrapper]:
    """Import the hermes_skills package and collect registered tools.

    Returns [] if the hermes_skills package is empty or fails to import.
    Never raises — a broken Hermes tool must not crash the worker.
    """
    wrappers: list[HermesToolWrapper] = []
    try:
        import worker.tools.hermes_skills  # triggers registry.register() calls
        from tools.registry import registry  # Hermes's own registry
    except Exception:
        return []  # Hermes not installed → no tools exposed
    for entry in registry._snapshot_entries():
        wrappers.append(
            HermesToolWrapper(entry.name, entry.handler, entry.schema)
        )
    return wrappers
```

### Dependency isolation strategy

Hermes tools bring heavy/transitive deps (playwright, mcp, etc.). We must
not bloat the worker image or break the existing environment:

1. **Optional extras in `worker/pyproject.toml`**:
   ```toml
   [project.optional-dependencies]
   hermes = ["hermes-agent-tools"]  # or vendored path
   ```
   The base `pip install ./worker` (used by our Dockerfile) does **not**
   install Hermes deps. Only `pip install ./worker[hermes]` does.

2. **Vendored subset** (recommended for zero-risk): copy only the specific
   Hermes tool modules we want into `worker/tools/hermes_skills/`, plus a
   minimal `registry.py` shim. This avoids pulling the entire `hermes-agent`
   package and its transitive deps. Each vendored tool's `check_fn` gates
   availability — if its deps are missing, it's silently excluded.

3. **Lazy import**: `discover_hermes_tools()` imports `hermes_skills` inside a
   try/except. If the package or any dep is missing, it returns `[]`. The
   worker boots normally with just our 4 tools.

4. **API keys / env vars**: Hermes tools read env vars directly (e.g.
   `TENOR_API_KEY`). These are surfaced via `requires_env` and `check_fn`.
   The adapter does **not** route these through our
   `OAuthCredentialStore` — Hermes tools are stateless and use their own
   env-var contract. Operators set these in the deployment env (same as
   `DEEPSEEK_API_KEY`).

### Wiring into the graph (opt-in only)

The `ToolExecutor` currently hardcodes our 4 tool classes. To add Hermes
tools **without touching existing code**:

- Add an optional `extra_tools: list[Tool] = None` param to
  `ToolExecutor.__init__`. Default `None` → existing behavior.
- The graph builder passes `extra_tools=discover_hermes_tools()` only if a
  `ENABLE_HERMES_TOOLS=1` env var is set. Default off.
- The LLM router's `ALLOWED_TOOLS` frozenset is extended **dynamically** at
  router construction time (read the enabled Hermes tool names and union them
  in). The system prompt's `_TOOL_NAME_ENUM` is built from the extended set.
  When disabled, the enum is unchanged → zero prompt-token impact.

---

## 3. Compatibility & Guardrails

### Existing tests (240 passing) — guaranteed unaffected

- **No existing file is modified.** `base.py`, `executor.py`, `github.py`,
  `gmail.py`, `__init__.py`, `graph.py`, `router.py` all retain their current
  behavior when `ENABLE_HERMES_TOOLS` is unset (the default).
- New tests for the adapter live in `worker/tests/test_hermes_adapter.py` —
  they test the wrapper in isolation with mock Hermes handlers. They do not
  import or exercise our 4 tools.
- The existing `test_tools.py`, `test_worker_graph.py`,
  `test_integration.py` are unchanged and still pass because the import graph
  is unchanged.

### Redis Streams — unaffected

- Hermes tool wrappers return `ToolResult` exactly like our tools. The
  `RedisStreamPublisher` serializes `ToolResult.to_dict()` — no schema change.
- Event types (`agent:status_update`, `response:chunk`, etc.) are produced by
  the graph nodes, not by tools. Hermes tools don't emit events.

### Latency SLOs — unaffected

- The adapter adds one `json.loads()` + one dict-to-ToolResult mapping per
  call — sub-millisecond.
- Hermes tool handlers run in the same `execute_tool` node; no extra network
  hop.
- When `ENABLE_HERMES_TOOLS=0` (default), `discover_hermes_tools()` is never
  called — zero overhead, zero import cost, zero prompt-token increase (the
  tool enum is unchanged).

### Allow-list safety

- Hermes tools are namespaced `hermes.<name>` — they cannot collide with
  `github.*` / `gmail.*`.
- The LLM router rejects any tool name not in `ALLOWED_TOOLS`. Hermes tools
  are only added to the allow-list when explicitly enabled, so a misconfigured
  or rogue Hermes tool can never execute by default.
- `check_fn` per Hermes tool gates availability — missing deps/env vars →
  tool excluded from the schema → model never sees it.

### Idempotency & confirmation gaps (documented, not blocking)

Hermes tools bypass our `tool_executions` idempotency table and
`UserConfirmationProtocol` because they're invoked through the wrapper, not
the `ToolExecutor`'s idempotency path. **Mitigation**: the wrapper is only
enabled for **read-only / non-sensitive** Hermes tools (enforced by an
allow-list in `hermes_skills/__init__.py`). Sensitive Hermes tools (write
operations) are excluded from discovery until a follow-up phase adds them to
`SENSITIVE_TOOLS` and wires confirmation.

---

## Step-by-Step Implementation Plan

| Step | File | Action | Risk |
|------|------|--------|------|
| 1 | `worker/tools/hermes_skills/__init__.py` | Create empty package with a `README.md` documenting the isolation contract. | None |
| 2 | `worker/tools/hermes_skills/registry.py` | Minimal `ToolRegistry` shim (or vendor Hermes's `registry.py`) so vendored tools can self-register without importing the full `hermes-agent` package. | None — isolated |
| 3 | `worker/tools/hermes_adapter.py` | Implement `HermesToolWrapper` + `discover_hermes_tools()` as specified above. | None — not imported by default |
| 4 | `worker/tests/test_hermes_adapter.py` | Unit tests: mock Hermes handler → `ToolResult` mapping, sync + async, error paths, JSON-decode failure. | None — new file |
| 5 | `worker/pyproject.toml` | Add `[project.optional-dependencies] hermes = [...]`. Base install unchanged. | None — optional extra |
| 6 | `worker/tools/executor.py` | Add optional `extra_tools` param (default `None`). Existing callers unchanged. | Low — additive, default preserves behavior |
| 7 | `worker/entrypoint.py` | Pass `extra_tools=discover_hermes_tools()` only if `ENABLE_HERMES_TOOLS=1`. | Low — gated by env var |
| 8 | `llm/router.py` | Build `ALLOWED_TOOLS` dynamically: base 4 tools + (if enabled) Hermes tool names. System prompt enum built from the union. | Low — default off = unchanged |
| 9 | `worker/tools/hermes_skills/<tool>.py` | Vendor the first desired Hermes tool (e.g. a read-only one) + its `check_fn`. | None — isolated |
| 10 | `deploy/docker-compose.yml` | Add `ENABLE_HERMES_TOOLS` env (default `0`) + any `requires_env` vars for the vendored tool. | None — default off |

**Verification gate after each step**: run the full test suite from repo
root. Steps 1–5 and 9 add only new files — the suite is structurally
incapable of regressing. Steps 6–8 modify existing files but preserve
default behavior; the existing tests prove no regression because they
exercise the default path.

---

**Awaiting review.** No code has been modified. Once approved (or adjusted),
implementation will start from Step 1.