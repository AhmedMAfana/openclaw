"""Fitness function: MCP tool schemas have no ambient-identifier args.

Constitution Principle III (NON-NEGOTIABLE): "No tool exposed to a
coding agent accepts a project, container, workspace, or instance
identifier as an argument." If the agent can name a target, it can
target the wrong one.

Walks every ``mcp_servers/*.py`` for ``@mcp.tool()`` decorated async
functions and asserts none of their parameter names contain the
forbidden substrings ``instance``, ``project``, ``workspace``, or
``container``.

This is the same check as the T033 unit test, lifted into the fitness
framework so it runs independently of pytest infrastructure and
becomes a CI gate without a test runner.
"""
from __future__ import annotations

import ast
import pathlib

from scripts.fitness import Finding, FitnessResult, Severity


REPO = pathlib.Path(__file__).resolve().parents[2]
MCP_DIR = REPO / "src" / "taghdev" / "mcp_servers"

_FORBIDDEN = ("instance", "project", "workspace", "container")
# These tool files are NOT scoped to a single instance — they're the
# legacy host/docker MCPs that DO take container_name args. Skip them.
_LEGACY_FILES = {"docker_mcp.py", "host_mcp.py", "github_mcp.py", "actions_mcp.py", "project_info.py"}


def check() -> FitnessResult:
    findings: list[Finding] = []
    result = FitnessResult(
        name="no_ambient_args",
        principles=["III"],
        description="Per-instance MCP tool schemas accept no ambient-identifier arguments.",
        passed=True,
    )

    if not MCP_DIR.is_dir():
        result.error = f"missing {MCP_DIR}"
        result.passed = False
        return result

    for path in sorted(MCP_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        if path.name in _LEGACY_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            findings.append(Finding(
                severity=Severity.HIGH,
                message=f"could not parse {path.name}: {e}",
                location=str(path.relative_to(REPO)),
            ))
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
            if not is_tool:
                continue
            for arg in (*node.args.args, *node.args.kwonlyargs):
                low = arg.arg.lower()
                if any(tok in low for tok in _FORBIDDEN):
                    findings.append(Finding(
                        severity=Severity.CRITICAL,
                        message=(
                            f"@mcp.tool {node.name!r} has parameter {arg.arg!r} — "
                            f"contains a forbidden ambient-identifier substring "
                            f"({_FORBIDDEN}). Principle III violation: an LLM "
                            f"that can name a target can target the wrong one."
                        ),
                        location=f"{path.relative_to(REPO)}:{node.lineno}",
                    ))

    result.findings = findings
    result.passed = result.critical_count == 0
    return result
