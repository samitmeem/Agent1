"""Tests for the Hermes adapter — ``worker.tools.hermes_adapter``.

These tests exercise the wrapper in isolation with mock Hermes handlers.
They do **not** import or exercise our 4 production tools (github/gmail),
so they cannot regress the existing suite.

Coverage:
- Sync handler returning a JSON string → ``ToolResult(success=True)``.
- Async handler returning a coroutine → awaited correctly.
- Handler returning a dict directly (no JSON encoding).
- Handler returning an error dict → ``ToolResult(success=False)``.
- Handler raising an exception → ``ToolResult(success=False)``.
- Handler returning non-JSON string → ``ToolResult(success=False)``.
- ``discover_hermes_tools()`` returns ``[]`` when the package is missing.
- ``discover_hermes_tools()`` wraps registered tools from the shim registry.
- Tool names are namespaced as ``hermes.<name>``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from contracts.models import ToolPayload
from worker.tools.base import ToolResult
from worker.tools.hermes_adapter import (
    HermesToolWrapper,
    discover_hermes_tools,
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _make_payload(**overrides: Any) -> ToolPayload:
    """Build a minimal ``ToolPayload`` for testing."""
    defaults: dict[str, Any] = {
        "execution_id": uuid4(),
        "ticket_id": uuid4(),
        "user_id": uuid4(),
        "tool_name": "hermes.test_tool",
        "idempotency_key": "test-key-1234567890",
        "arguments": {"query": "hello"},
        "oauth_connection_id": None,
        "dry_run": False,
    }
    defaults.update(overrides)
    return ToolPayload(**defaults)


def _sync_handler_ok(args: dict, **kw: Any) -> str:
    """Sync handler returning a success JSON string."""
    return json.dumps({"result": args.get("query", ""), "count": 42})


async def _async_handler_ok(args: dict, **kw: Any) -> str:
    """Async handler returning a success JSON string."""
    await asyncio.sleep(0)
    return json.dumps({"result": args.get("query", ""), "async": True})


def _sync_handler_dict(args: dict, **kw: Any) -> dict:
    """Sync handler returning a dict directly (no JSON encoding)."""
    return {"result": "direct", "items": [1, 2, 3]}


def _sync_handler_error(args: dict, **kw: Any) -> str:
    """Sync handler returning an error JSON string."""
    return json.dumps({"error": "something went wrong"})


def _sync_handler_raises(args: dict, **kw: Any) -> str:
    """Sync handler that raises an exception."""
    raise RuntimeError("boom")


def _sync_handler_non_json(args: dict, **kw: Any) -> str:
    """Sync handler returning a non-JSON string."""
    return "this is not json"


# ---------------------------------------------------------------------------
# HermesToolWrapper — sync handler.
# ---------------------------------------------------------------------------

class TestSyncHandler:
    def test_sync_success_json_string(self):
        wrapper = HermesToolWrapper(
            hermes_name="test_tool",
            handler=_sync_handler_ok,
            schema={"name": "test_tool", "description": "test"},
        )
        assert wrapper.name == "hermes.test_tool"
        payload = _make_payload()

        result = asyncio.run(wrapper._invoke(payload, None))

        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.tool_name == "hermes.test_tool"
        assert result.data == {"result": "hello", "count": 42}

    def test_sync_success_dict_result(self):
        wrapper = HermesToolWrapper(
            hermes_name="dict_tool",
            handler=_sync_handler_dict,
            schema={"name": "dict_tool"},
        )
        payload = _make_payload()

        result = asyncio.run(wrapper._invoke(payload, None))

        assert result.success is True
        assert result.data == {"result": "direct", "items": [1, 2, 3]}

    def test_sync_error_dict(self):
        wrapper = HermesToolWrapper(
            hermes_name="err_tool",
            handler=_sync_handler_error,
            schema={"name": "err_tool"},
        )
        payload = _make_payload()

        result = asyncio.run(wrapper._invoke(payload, None))

        assert result.success is False
        assert result.tool_name == "hermes.err_tool"
        assert "something went wrong" in (result.error or "")
        assert result.data == {"error": "something went wrong"}

    def test_sync_handler_raises(self):
        wrapper = HermesToolWrapper(
            hermes_name="raise_tool",
            handler=_sync_handler_raises,
            schema={"name": "raise_tool"},
        )
        payload = _make_payload()

        result = asyncio.run(wrapper._invoke(payload, None))

        assert result.success is False
        assert "hermes tool error" in (result.error or "")
        assert "boom" in (result.error or "")

    def test_sync_non_json_result(self):
        wrapper = HermesToolWrapper(
            hermes_name="nonjson_tool",
            handler=_sync_handler_non_json,
            schema={"name": "nonjson_tool"},
        )
        payload = _make_payload()

        result = asyncio.run(wrapper._invoke(payload, None))

        assert result.success is False
        assert "non-JSON" in (result.error or "")


# ---------------------------------------------------------------------------
# HermesToolWrapper — async handler.
# ---------------------------------------------------------------------------

class TestAsyncHandler:
    def test_async_success(self):
        wrapper = HermesToolWrapper(
            hermes_name="async_tool",
            handler=_async_handler_ok,
            schema={"name": "async_tool"},
            is_async=True,
        )
        payload = _make_payload(arguments={"query": "world"})

        result = asyncio.run(wrapper._invoke(payload, None))

        assert result.success is True
        assert result.data == {"result": "world", "async": True}

    def test_async_detected_via_coroutine_without_flag(self):
        """Even without is_async=True, a coroutine result is awaited."""
        wrapper = HermesToolWrapper(
            hermes_name="async_auto",
            handler=_async_handler_ok,
            schema={"name": "async_auto"},
            is_async=False,
        )
        payload = _make_payload()

        result = asyncio.run(wrapper._invoke(payload, None))

        assert result.success is True
        assert result.data["async"] is True


# ---------------------------------------------------------------------------
# Namespacing.
# ---------------------------------------------------------------------------

class TestNamespacing:
    def test_name_prefixed_with_hermes(self):
        wrapper = HermesToolWrapper(
            hermes_name="weather",
            handler=_sync_handler_ok,
            schema={"name": "weather"},
        )
        assert wrapper.name == "hermes.weather"

    def test_no_collision_with_github(self):
        wrapper = HermesToolWrapper(
            hermes_name="github.create_issue",
            handler=_sync_handler_ok,
            schema={"name": "github.create_issue"},
        )
        # Even if a Hermes tool is named "github.create_issue", it becomes
        # "hermes.github.create_issue" — no collision with our tool.
        assert wrapper.name == "hermes.github.create_issue"
        assert wrapper.name != "github.create_issue"


# ---------------------------------------------------------------------------
# discover_hermes_tools().
# ---------------------------------------------------------------------------

class TestDiscoverHermesTools:
    def test_returns_empty_when_package_missing(self):
        """If hermes_skills can't be imported, returns [] without raising."""
        with patch.dict(
            "sys.modules",
            {"worker.tools.hermes_skills": None},
            clear=False,
        ):
            # Force ImportError on the package.
            import builtins

            real_import = builtins.__import__

            def _block_hermes(name, *args, **kwargs):
                if name == "worker.tools.hermes_skills":
                    raise ImportError("blocked for test")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=_block_hermes):
                result = discover_hermes_tools()
            assert result == []

    def test_returns_empty_when_registry_empty(self):
        """An empty shim registry yields no wrappers."""
        from worker.tools.hermes_skills.registry import registry as shim

        # Save and clear.
        saved = shim._snapshot_entries()
        for entry in saved:
            shim.deregister(entry.name)
        try:
            result = discover_hermes_tools()
            assert result == []
        finally:
            # Restore.
            for entry in saved:
                shim.register(
                    name=entry.name,
                    toolset=entry.toolset,
                    schema=entry.schema,
                    handler=entry.handler,
                    check_fn=entry.check_fn,
                    requires_env=entry.requires_env,
                    is_async=entry.is_async,
                    description=entry.description,
                    emoji=entry.emoji,
                    max_result_size_chars=entry.max_result_size_chars,
                )

    def test_wraps_registered_tools(self):
        """Tools registered in the shim are discovered and wrapped."""
        from worker.tools.hermes_skills.registry import registry as shim

        shim.register(
            name="discover_test",
            toolset="test",
            schema={"name": "discover_test", "description": "test"},
            handler=_sync_handler_ok,
        )
        try:
            wrappers = discover_hermes_tools()
            names = [w.name for w in wrappers]
            assert "hermes.discover_test" in names
        finally:
            shim.deregister("discover_test")

    def test_never_raises_on_broken_tool(self):
        """A tool that fails to wrap is skipped, not fatal."""
        from worker.tools.hermes_skills.registry import registry as shim

        # Register a tool whose handler is not callable to trigger a wrap
        # failure inside HermesToolWrapper.__init__ (super().__init__ with
        # http_client=None creates a real client, so we patch to raise).
        shim.register(
            name="broken_tool",
            toolset="test",
            schema={"name": "broken_tool"},
            handler=_sync_handler_ok,
        )
        try:
            with patch(
                "worker.tools.hermes_adapter.HermesToolWrapper.__init__",
                side_effect=RuntimeError("init failed"),
            ):
                wrappers = discover_hermes_tools()
            # The broken tool is skipped; others (if any) may still appear.
            assert all(w.name != "hermes.broken_tool" for w in wrappers)
        finally:
            shim.deregister("broken_tool")


# ---------------------------------------------------------------------------
# Shim registry.
# ---------------------------------------------------------------------------

class TestShimRegistry:
    def test_register_and_snapshot(self):
        from worker.tools.hermes_skills.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register(
            name="x",
            toolset="t",
            schema={"name": "x"},
            handler=lambda args, **kw: json.dumps({"ok": True}),
        )
        entries = reg._snapshot_entries()
        assert len(entries) == 1
        assert entries[0].name == "x"

    def test_dispatch_sync(self):
        from worker.tools.hermes_skills.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register(
            name="d",
            toolset="t",
            schema={"name": "d"},
            handler=lambda args, **kw: json.dumps({"echo": args.get("v")}),
        )
        result = json.loads(reg.dispatch("d", {"v": 42}))
        assert result == {"echo": 42}

    def test_dispatch_unknown_returns_error(self):
        from worker.tools.hermes_skills.registry import ToolRegistry

        reg = ToolRegistry()
        result = json.loads(reg.dispatch("nope", {}))
        assert "error" in result

    def test_dispatch_handler_raises_returns_error(self):
        from worker.tools.hermes_skills.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register(
            name="boom",
            toolset="t",
            schema={"name": "boom"},
            handler=lambda args, **kw: (_ for _ in ()).throw(ValueError("x")),
        )
        result = json.loads(reg.dispatch("boom", {}))
        assert "error" in result

    def test_deregister(self):
        from worker.tools.hermes_skills.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register(
            name="rm",
            toolset="t",
            schema={"name": "rm"},
            handler=lambda args, **kw: "{}",
        )
        assert reg.get_entry("rm") is not None
        reg.deregister("rm")
        assert reg.get_entry("rm") is None


# ---------------------------------------------------------------------------
# End-to-end: vendored echo tool.
# ---------------------------------------------------------------------------

class TestVendoredEchoTool:
    """Verify the vendored ``echo`` tool is discovered and executable."""

    def test_echo_discovered_via_adapter(self):
        """``discover_hermes_tools()`` finds the vendored echo tool."""
        wrappers = discover_hermes_tools()
        names = [w.name for w in wrappers]
        assert "hermes.echo" in names

    def test_echo_executes_successfully(self):
        """The wrapped echo tool returns the expected ToolResult."""
        wrappers = discover_hermes_tools()
        echo_wrapper = next(w for w in wrappers if w.name == "hermes.echo")
        payload = _make_payload(
            tool_name="hermes.echo",
            arguments={"text": "hello world"},
        )

        result = asyncio.run(echo_wrapper._invoke(payload, None))

        assert result.success is True
        assert result.tool_name == "hermes.echo"
        assert result.data == {"echoed": "hello world", "length": 11}

    def test_echo_registered_in_shim(self):
        """The echo tool self-registers into the shim on package import."""
        from worker.tools.hermes_skills.registry import registry as shim

        entry = shim.get_entry("echo")
        assert entry is not None
        assert entry.toolset == "diagnostics"
        assert entry.check_fn is not None
        assert entry.check_fn() is True

    def test_echo_check_fn_true(self):
        """The echo tool's check_fn returns True (no external deps)."""
        from worker.tools.hermes_skills.echo_tool import _check_echo_requirements

        assert _check_echo_requirements() is True
