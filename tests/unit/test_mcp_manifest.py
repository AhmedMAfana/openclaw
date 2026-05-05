"""T033: MCP manifests MUST NOT expose ambient-identifier arguments.

Principle III enforcement (research.md §12 test #2). The per-task MCP
fleet is bound to ONE instance at argv-spawn time. Tools on that fleet
MUST NOT accept an instance / project / workspace / container argument:
accepting one would reintroduce the ambient authority the design is
specifically eliminating.

The test renders the tool manifests for ``instance_mcp``, ``workspace_mcp``,
and ``git_mcp`` (after the T040 extension) and asserts no tool has an
argument whose name contains any of the forbidden tokens.

Requires T038 (instance_mcp), T039 (workspace_mcp), and T040 (git_mcp
extension). Until they land the module skips cleanly.
"""
from __future__ import annotations

import pytest

_instance_mcp = pytest.importorskip(
    "taghdev.mcp_servers.instance_mcp",
    reason="T038 instance_mcp not landed yet",
)
_workspace_mcp = pytest.importorskip(
    "taghdev.mcp_servers.workspace_mcp",
    reason="T039 workspace_mcp not landed yet",
)
_git_mcp = pytest.importorskip(
    "taghdev.mcp_servers.git_mcp",
    reason="T040 git_mcp extension not landed yet",
)


FORBIDDEN_SUBSTRINGS = ("instance", "project", "workspace", "container")


def _render_tool_manifest(module) -> list[dict]:
    """Best-effort: look for `get_tool_manifest()` or `TOOLS` export.

    T038/T039/T040 implementations MUST export one of these. Failing
    that, the test fails loudly — the point is to force the contract,
    not to silently pass.
    """
    for name in ("get_tool_manifest", "TOOLS", "tool_manifest"):
        obj = getattr(module, name, None)
        if obj is None:
            continue
        tools = obj() if callable(obj) else obj
        if isinstance(tools, list):
            return tools
    raise AssertionError(
        f"{module.__name__} exports no tool manifest "
        f"(expected `get_tool_manifest()`, `TOOLS`, or `tool_manifest`)"
    )


@pytest.mark.parametrize(
    "module",
    [_instance_mcp, _workspace_mcp, _git_mcp],
    ids=lambda m: m.__name__,
)
def test_no_tool_has_ambient_identifier_argument(module):
    """Every tool's schema has zero args whose name contains a forbidden token."""
    manifest = _render_tool_manifest(module)
    offenders: list[tuple[str, str]] = []
    for tool in manifest:
        tool_name = tool.get("name") or tool.get("tool_name") or "<anon>"
        # Support both JSON-Schema style and `inputSchema.properties` style.
        schema = (
            tool.get("inputSchema")
            or tool.get("input_schema")
            or tool.get("parameters")
            or {}
        )
        props = schema.get("properties", {})
        for arg_name in props:
            low = arg_name.lower()
            if any(tok in low for tok in FORBIDDEN_SUBSTRINGS):
                offenders.append((tool_name, arg_name))

    assert not offenders, (
        f"{module.__name__}: tools expose ambient-identifier args "
        f"(Principle III violation): {offenders}"
    )
