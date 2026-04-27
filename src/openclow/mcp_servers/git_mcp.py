"""Git MCP Server — safe wrapper for git operations.

Returns errors as text (never non-zero exit codes) so the Claude Agent SDK
doesn't crash. The external mcp-server-git crashes the SDK on non-zero exits.

Two modes (T040):

* **Legacy** — positional argv[1] is the workspace path. No branch pinning.
  Used by existing host-mode and docker-mode agents in claude.py.

* **Pinned** — flag-based ``--workspace <path> --branch <name>``. When
  ``--branch`` is set, destructive ops (``git_checkout``, ``git_reset
  --hard <ref>``, ``git_branch -D``) are not exposed at all, and
  ``git_commit`` / ``git_push`` verify that ``HEAD`` still points at
  ``<branch>`` before acting. Per-instance chats use this mode via the
  ``_mcp_git_pinned`` factory in ``providers/llm/claude.py`` (T041).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from mcp.server.fastmcp import FastMCP

from openclow.settings import settings

mcp = FastMCP("git")


# ---------------------------------------------------------------------------
# Argv parsing — supports both the legacy positional form and the new
# flag form introduced in T040. Flags take precedence when present.
# ---------------------------------------------------------------------------


def _parse_argv(argv: list[str]) -> tuple[str, str | None]:
    parser = argparse.ArgumentParser(
        prog="openclow.mcp_servers.git_mcp",
        add_help=False,
    )
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--branch", default=None)
    ns, rest = parser.parse_known_args(argv)
    workspace = ns.workspace
    if not workspace:
        # Legacy: first positional that is an existing directory.
        for token in rest:
            if not token.startswith("-") and os.path.isdir(token):
                workspace = token
                break
    if not workspace:
        workspace = settings.workspace_base_path
    return workspace, ns.branch


_workspace, _pinned_branch = _parse_argv(sys.argv[1:])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_GIT_TIMEOUT_S = 30
_OUTPUT_MAX = 5000


async def _run_git(*args: str, cwd: str | None = None) -> str:
    """Run a git command safely. Always returns output, never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_GIT_TIMEOUT_S
        )
        output = (stdout.decode() + stderr.decode()).strip()
        if proc.returncode != 0:
            return f"FAILED (exit code {proc.returncode}):\n{output[-_OUTPUT_MAX:]}"
        return output[-_OUTPUT_MAX:] if output else "(no output)"
    except asyncio.TimeoutError:
        return f"FAILED: git command timed out ({_GIT_TIMEOUT_S}s)"
    except FileNotFoundError:
        return "FAILED: git binary not available"
    except Exception as e:
        return f"FAILED: {str(e)[:200]}"


async def _current_branch() -> str | None:
    """Return the short branch name, or None if detached / error."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--abbrev-ref", "HEAD",
            cwd=_workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_GIT_TIMEOUT_S
        )
        if proc.returncode != 0:
            return None
        name = stdout.decode().strip()
        return name or None
    except Exception:
        return None


async def _ensure_on_pinned_branch() -> str | None:
    """Guard: when pinned, refuse mutating ops if HEAD drifted off the branch."""
    if _pinned_branch is None:
        return None
    current = await _current_branch()
    if current != _pinned_branch:
        return (
            f"REFUSED: this git server is pinned to branch "
            f"{_pinned_branch!r} but HEAD is currently {current!r}. "
            "Switch back to the pinned branch to continue."
        )
    return None


# ---------------------------------------------------------------------------
# Tools — arg names avoid the forbidden substrings (T033).
# ---------------------------------------------------------------------------


@mcp.tool()
async def git_status() -> str:
    """Show git status — modified, staged, untracked files."""
    return await _run_git("status", "--short", cwd=_workspace)


@mcp.tool()
async def git_diff() -> str:
    """Show the diff of unstaged changes (working tree vs index)."""
    return await _run_git("diff", cwd=_workspace)


@mcp.tool()
async def git_diff_staged() -> str:
    """Show the diff of staged changes (what will be committed)."""
    return await _run_git("diff", "--cached", cwd=_workspace)


@mcp.tool()
async def git_diff_unstaged() -> str:
    """Alias for git_diff — kept for backwards compatibility."""
    return await _run_git("diff", cwd=_workspace)


@mcp.tool()
async def git_add(path: str = ".") -> str:
    """Stage files for commit. Use '.' for all changes."""
    return await _run_git("add", path, cwd=_workspace)


@mcp.tool()
async def git_log(count: int = 10) -> str:
    """Show recent git commits."""
    return await _run_git("log", "--oneline", f"-{max(1, int(count or 10))}", cwd=_workspace)


@mcp.tool()
async def git_show(ref: str = "HEAD") -> str:
    """Show a specific commit's changes."""
    return await _run_git("show", ref, "--stat", cwd=_workspace)


@mcp.tool()
async def git_commit(message: str) -> str:
    """Commit staged changes with `message`.

    When this server is pinned to a branch, refuses if HEAD has drifted
    off that branch — prevents committing to a branch the chat's session
    does not own.
    """
    if not message or not message.strip():
        return "FAILED: commit message must be non-empty"
    guard = await _ensure_on_pinned_branch()
    if guard:
        return guard
    return await _run_git("commit", "-m", message, cwd=_workspace)


@mcp.tool()
async def git_push(remote: str = "origin") -> str:
    """Push the pinned branch to `remote`.

    When pinned, the branch name is fixed at spawn — this tool never
    takes a branch argument, so an agent cannot redirect the push to
    another branch. In legacy mode (no pinning) it pushes HEAD.
    """
    guard = await _ensure_on_pinned_branch()
    if guard:
        return guard
    if _pinned_branch is not None:
        return await _run_git(
            "push", remote, f"HEAD:{_pinned_branch}", cwd=_workspace
        )
    return await _run_git("push", remote, "HEAD", cwd=_workspace)


# Deliberately NOT exposed under pinned mode: git_checkout, git_reset,
# git_branch_delete. An agent cannot switch off the pinned branch because
# the tools simply do not exist. Per T040: "refused if the resulting HEAD
# would not be <branch>" — in pinned mode we treat "would not be" as the
# degenerate "cannot guarantee", so the whole family is omitted.


def get_tool_manifest() -> list[dict]:
    """T033 helper — see ``instance_mcp.get_tool_manifest``."""
    return [
        {"name": t.name, "parameters": t.parameters}
        for t in mcp._tool_manager.list_tools()
    ]


if __name__ == "__main__":
    mcp.run(transport="stdio")
