"""Fitness function: ``CONTAINER_MODE_TOOLS`` ↔ actual MCP tool registrations.

Catches the bug class where ``providers/llm/claude.py`` declares an
``allowed_tools`` list with a ``mcp__<server>__<tool>`` string that
doesn't correspond to any ``@mcp.tool()`` registered on that MCP
server's module — the agent would request a tool the SDK doesn't
have, surfaces as a confusing runtime error or silently no-op.

Walks:

  * ``providers/llm/claude.py`` for the ``CONTAINER_MODE_TOOLS``
    list literal (and any other obvious ``allowed_tools=[...]`` lists
    we recognise).
  * ``mcp_servers/instance_mcp.py`` / ``workspace_mcp.py`` /
    ``git_mcp.py`` (the per-instance trio bound to one chat) for
    every ``@mcp.tool()`` decorated async function.
  * Diffs:
      - tool name in the allowlist with no registration → CRITICAL
      - registration with no allowlist entry → INFO (might be
        optional; legacy git_mcp tools that pre-date container mode)

Maps to Constitution Principle III (No Ambient Authority — pinning
the tool surface at spawn time is meaningless if half the names are
phantoms) and Principle VII (Verified Work).
"""
from __future__ import annotations

import ast
import pathlib

from scripts.fitness import Finding, FitnessResult, Severity


REPO = pathlib.Path(__file__).resolve().parents[2]
CLAUDE = REPO / "src" / "openclow" / "providers" / "llm" / "claude.py"
MCP_DIR = REPO / "src" / "openclow" / "mcp_servers"

# The per-instance trio that the container-mode allowlist binds to.
# Each entry is (mcp_server_name_in_url, source_file).
_PINNED_SERVERS: dict[str, str] = {
    "instance": "instance_mcp.py",
    "workspace": "workspace_mcp.py",
    "git": "git_mcp.py",
}

# Tool names that are legitimately registered but optional in the
# container-mode allowlist (e.g. legacy git_mcp tools that exist for
# host/docker-mode chats, kept available but not promoted).
_REGISTERED_OPTIONAL: set[str] = {
    "mcp__git__git_diff_unstaged",  # alias for git_diff, kept for legacy callers
}

# Built-in (non-MCP) tools that aren't subject to this contract — the
# Claude Agent SDK provides them natively. Filter out before the
# diff so they don't show up as "missing registrations".
_NATIVE_TOOLS: set[str] = {
    "Read", "Write", "Edit", "Glob", "Grep", "Task",
    "WebFetch", "WebSearch", "Bash",
}


def check() -> FitnessResult:
    findings: list[Finding] = []
    result = FitnessResult(
        name="mcp_tool_contract",
        principles=["III", "VII"],
        description="CONTAINER_MODE_TOOLS strings match @mcp.tool() registrations on the pinned MCP servers.",
        passed=True,
    )

    if not CLAUDE.is_file():
        result.error = f"missing {CLAUDE}"
        result.passed = False
        return result

    declared = _container_mode_tools()
    if declared is None:
        result.error = "could not parse CONTAINER_MODE_TOOLS list literal"
        result.passed = False
        return result

    # Filter out native-built-in tools from the contract surface.
    mcp_declared = {t for t in declared if t.startswith("mcp__")}

    registered = _registered_tools()

    # Class A: declared but not registered → CRITICAL
    missing = mcp_declared - registered
    for t in sorted(missing):
        findings.append(Finding(
            severity=Severity.CRITICAL,
            message=(
                f"CONTAINER_MODE_TOOLS lists `{t}` but the underlying "
                f"@mcp.tool() registration was not found on the pinned "
                f"server. Either fix the typo, register the tool, or "
                f"remove it from the allowlist."
            ),
            location=f"{CLAUDE.relative_to(REPO)}",
        ))

    # Class B: registered but not declared → INFO
    extra = registered - mcp_declared - _REGISTERED_OPTIONAL
    for t in sorted(extra):
        findings.append(Finding(
            severity=Severity.INFO,
            message=(
                f"`{t}` is registered on a pinned MCP server but is NOT "
                f"in CONTAINER_MODE_TOOLS — agents in container-mode "
                f"chats cannot call it. Add to CONTAINER_MODE_TOOLS or "
                f"to _REGISTERED_OPTIONAL with a one-line reason."
            ),
            location="src/openclow/mcp_servers/",
        ))

    result.findings = findings
    result.passed = result.critical_count == 0
    return result


# --- helpers ---------------------------------------------------------


def _container_mode_tools() -> set[str] | None:
    """Walk claude.py for the CONTAINER_MODE_TOOLS list literal."""
    try:
        tree = ast.parse(CLAUDE.read_text(encoding="utf-8"))
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        target_id = None
        value = None
        if isinstance(node, ast.Assign):
            if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "CONTAINER_MODE_TOOLS"):
                target_id = node.targets[0].id
                value = node.value
        elif isinstance(node, ast.AnnAssign):
            if (isinstance(node.target, ast.Name)
                    and node.target.id == "CONTAINER_MODE_TOOLS"
                    and node.value is not None):
                target_id = node.target.id
                value = node.value
        if target_id is None or not isinstance(value, ast.List):
            continue
        out: set[str] = set()
        for elt in value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.add(elt.value)
        return out
    return None


def _registered_tools() -> set[str]:
    """Walk each pinned MCP server file for @mcp.tool() registrations.

    Returns the set of qualified ``mcp__<server>__<tool>`` names so it
    can be set-diffed against CONTAINER_MODE_TOOLS directly.
    """
    out: set[str] = set()
    for server_name, filename in _PINNED_SERVERS.items():
        path = MCP_DIR / filename
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            is_tool = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr == "tool"
                for d in node.decorator_list
            )
            if is_tool:
                out.add(f"mcp__{server_name}__{node.name}")
    return out
