"""Senior-DevOps refactor — Change 2: assert the lifecycle MCP tools
(`provision_now`, `instance_status`, `terminate_now`) are registered on
``instance_mcp`` and accept zero ambient-identifier arguments.

The tools' DB-backed runtime behaviour is exercised in integration
tests; this unit gate is the cheap, fast wire-up check that catches
regressions without spinning up Postgres.
"""
from __future__ import annotations

import pytest

instance_mcp = pytest.importorskip("openclow.mcp_servers.instance_mcp")


_FORBIDDEN = ("instance", "project", "workspace", "container", "chat")


def _manifest_by_name() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for tool in instance_mcp.get_tool_manifest():
        out[tool["name"]] = tool
    return out


def test_lifecycle_tools_registered():
    names = set(_manifest_by_name().keys())
    assert "provision_now" in names
    assert "instance_status" in names
    assert "terminate_now" in names


def test_lifecycle_tools_have_no_ambient_args():
    """Principle III — argv pinning, not tool args.

    All three lifecycle tools resolve the active instance via the
    pinned ``--chat-session-id`` argv; their tool schemas must take
    zero arguments so an LLM can't substitute someone else's chat at
    call time.
    """
    manifest = _manifest_by_name()
    offenders: list[tuple[str, str]] = []
    for tool_name in ("provision_now", "instance_status", "terminate_now"):
        tool = manifest.get(tool_name)
        if tool is None:
            pytest.fail(f"missing tool: {tool_name}")
        schema = (
            tool.get("inputSchema")
            or tool.get("input_schema")
            or tool.get("parameters")
            or {}
        )
        props = schema.get("properties", {}) or {}
        for arg_name in props:
            low = arg_name.lower()
            if any(tok in low for tok in _FORBIDDEN):
                offenders.append((tool_name, arg_name))
    assert not offenders, (
        f"lifecycle tools expose ambient-identifier args "
        f"(Principle III): {offenders}"
    )


def test_lifecycle_tools_in_container_mode_allowlist():
    """The tool allowlist for container-mode chats must include the new tools."""
    from openclow.providers.llm.claude import CONTAINER_MODE_TOOLS
    assert "mcp__instance__provision_now" in CONTAINER_MODE_TOOLS
    assert "mcp__instance__instance_status" in CONTAINER_MODE_TOOLS
    assert "mcp__instance__terminate_now" in CONTAINER_MODE_TOOLS


def test_mcp_instance_factory_passes_chat_session_id():
    """Argv-pinning carries chat_session_id from the Instance row."""
    from openclow.providers.llm.claude import _mcp_instance

    class _FakeInstance:
        compose_project = "tagh-inst-deadbeef"
        chat_session_id = 4242

    spec = _mcp_instance(_FakeInstance())
    args = spec["args"]
    assert "--chat-session-id" in args
    idx = args.index("--chat-session-id")
    assert args[idx + 1] == "4242"
