"""Git MCP Server — safe wrapper for git operations.

Returns errors as text (never non-zero exit codes) so the Claude Agent SDK
doesn't crash. The external mcp-server-git crashes the SDK on non-zero exits.
"""
import asyncio

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("git")


async def _run_git(*args: str, cwd: str = None) -> str:
    """Run a git command safely. Always returns output, never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = (stdout.decode() + stderr.decode()).strip()
        if proc.returncode != 0:
            return f"FAILED (exit code {proc.returncode}):\n{output[-5000:]}"
        return output[-5000:] if output else "(no output)"
    except asyncio.TimeoutError:
        return "FAILED: git command timed out (30s)"
    except Exception as e:
        return f"FAILED: {str(e)[:200]}"


import os
import sys

from openclow.settings import settings

# Workspace path: callers (claude.py:_mcp_git) pass the per-task or per-project
# path as argv[1]. Fall back to the global workspace_base_path for safety, but
# that base is empty for host-mode projects and was the cause of every git tool
# call returning "fatal: not a git repository" — which silently broke the
# Reviewer agent until it hit max_turns and the CLI exited non-zero.
_workspace = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else settings.workspace_base_path


@mcp.tool()
async def git_status() -> str:
    """Show git status — modified, staged, untracked files."""
    return await _run_git("status", "--short", cwd=_workspace)


@mcp.tool()
async def git_diff_staged() -> str:
    """Show the diff of staged changes (what will be committed)."""
    return await _run_git("diff", "--cached", cwd=_workspace)


@mcp.tool()
async def git_diff_unstaged() -> str:
    """Show the diff of unstaged changes (working directory vs index)."""
    return await _run_git("diff", cwd=_workspace)


@mcp.tool()
async def git_add(path: str = ".") -> str:
    """Stage files for commit. Use '.' for all changes."""
    return await _run_git("add", path, cwd=_workspace)


@mcp.tool()
async def git_log(count: int = 10) -> str:
    """Show recent git commits."""
    return await _run_git("log", f"--oneline", f"-{count}", cwd=_workspace)


@mcp.tool()
async def git_show(ref: str = "HEAD") -> str:
    """Show a specific commit's changes."""
    return await _run_git("show", ref, "--stat", cwd=_workspace)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        _workspace = sys.argv[1]
    mcp.run(transport="stdio")
