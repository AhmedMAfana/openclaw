"""Host MCP Server — gives agents control over user apps that live as plain
directories on the VPS host (mode="host" projects).

All mutating commands go through host_guard for allowlist enforcement and audit
logging. Read-only helpers (check_port, process_status, tail_log) still route
through host_guard so every host tool call is audited.

The agent reaches these tools as mcp__host__<tool_name>. The sibling Docker
path via mcp__docker__* is untouched.
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from openclow.services.host_guard import run_host

mcp = FastMCP("host")


def _stream_channel() -> Optional[str]:
    """The orchestrator puts a `HOST_STREAM_CHANNEL` in the MCP server's env so
    long-running tool output can stream to the user's web chat panel. If the var
    is absent, tools fall back to buffering + returning the full output."""
    return os.environ.get("HOST_STREAM_CHANNEL") or None


@mcp.tool()
async def host_cd(project_dir: str) -> str:
    """Verify the project directory exists and return its canonical path + git status."""
    real = os.path.realpath(project_dir) if os.path.exists(project_dir) else project_dir
    if not os.path.isdir(real):
        return f"MISSING: {project_dir} does not exist"
    rc, out = await run_host(
        "git rev-parse --abbrev-ref HEAD && git status --porcelain | head -20",
        cwd=real, actor="mcp_host", timeout=10,
    )
    if rc != 0:
        return f"OK path={real} (not a git repo)\n{out[:500]}"
    return f"OK path={real}\nbranch+status:\n{out[:1000]}"


@mcp.tool()
async def host_git_clone(repo_url: str, project_dir: str, branch: str = "main") -> str:
    """First-time clone into project_dir. Parent directory must be under the
    configured host.projects_base. If project_dir already exists and is a git
    repo, this is a no-op."""
    parent = os.path.dirname(os.path.realpath(project_dir))
    if os.path.isdir(os.path.join(project_dir, ".git")):
        return f"OK already cloned at {project_dir}"
    os.makedirs(parent, exist_ok=True)
    rc, out = await run_host(
        f"git clone --branch {branch} {repo_url} {os.path.basename(project_dir)}",
        cwd=parent, actor="mcp_host", timeout=300,
        stream_channel=_stream_channel(),
    )
    if rc != 0:
        return f"FAILED (exit {rc}):\n{out[-3000:]}"
    return f"OK cloned to {project_dir}"


@mcp.tool()
async def host_git_pull(project_dir: str, branch: str = "main") -> str:
    """git fetch + reset to origin/<branch>. Idempotent; overwrites local changes."""
    rc, out = await run_host(
        f"git fetch origin {branch} && git reset --hard origin/{branch} && git log -1 --oneline",
        cwd=project_dir, actor="mcp_host", timeout=60,
        stream_channel=_stream_channel(),
    )
    if rc != 0:
        return f"FAILED (exit {rc}):\n{out[-2000:]}"
    return f"OK\n{out[-1000:]}"


@mcp.tool()
async def host_read_install_guide(project_dir: str) -> str:
    """Read the project's install guide. Tries in order:
    README.md, README.rst, INSTALL.md, SETUP.md, CLAUDE.md, docs/INSTALL.md.
    Returns the first ~8KB of the first file that exists."""
    candidates = [
        "README.md", "README.rst", "README.txt", "readme.md",
        "INSTALL.md", "INSTALL.txt", "SETUP.md",
        "CLAUDE.md", "docs/INSTALL.md", "docs/SETUP.md",
    ]
    for name in candidates:
        path = os.path.join(project_dir, name)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    body = f.read(8192)
                return f"FILE: {name}\n---\n{body}"
            except Exception as e:
                return f"FAILED to read {name}: {e}"
    return "MISSING: no install guide found (looked for README/INSTALL/SETUP/CLAUDE)"


@mcp.tool()
async def host_run_command(project_dir: str, command: str, timeout: int = 120) -> str:
    """Run a shell command inside the project directory on the host.

    Guarded by host_guard allowlist. Commands must start with one of the allowed
    binaries (git, npm, pip, php, composer, pm2, systemctl, curl, etc.). Output
    is streamed to the user's chat panel while the command runs (if the
    orchestrator set HOST_STREAM_CHANNEL), and the final combined stdout+stderr
    is returned truncated to the last 5KB.
    """
    rc, out = await run_host(
        command, cwd=project_dir, actor="mcp_host", timeout=timeout,
        stream_channel=_stream_channel(),
    )
    tag = "OK" if rc == 0 else f"FAILED exit={rc}"
    return f"{tag}\n{out[-5000:]}"


@mcp.tool()
async def host_check_port(port: int) -> str:
    """Check whether a TCP port is currently bound on localhost.
    Returns 'LISTEN <pid> <cmd>' if something is listening, else 'FREE'."""
    # ss first (universal on Linux), lsof fallback for macOS
    rc, out = await run_host(
        f"ss -ltnp 'sport = :{port}' 2>/dev/null || lsof -iTCP:{port} -sTCP:LISTEN -nP 2>/dev/null",
        cwd=os.environ.get("HOME", "/tmp"), actor="mcp_host", timeout=5,
    )
    if rc != 0 or not out.strip():
        return "FREE"
    # Compact: first data line
    for line in out.splitlines():
        if str(port) in line:
            return f"LISTEN {line.strip()[:200]}"
    return f"LISTEN\n{out[:400]}"


@mcp.tool()
async def host_curl(url: str, timeout: int = 10) -> str:
    """Hit an HTTP URL from the host. Returns 'HTTP <code>\\n<first 1KB body>'."""
    rc, out = await run_host(
        f"curl -sS -o - -w '\\n__HTTP_STATUS__:%{{http_code}}' --max-time {timeout} {url}",
        cwd=os.environ.get("HOME", "/tmp"), actor="mcp_host", timeout=timeout + 5,
    )
    if rc != 0 and "__HTTP_STATUS__" not in out:
        return f"FAILED\n{out[-500:]}"
    # parse trailing status
    code = "000"
    body = out
    if "__HTTP_STATUS__:" in out:
        body, _, tail = out.rpartition("__HTTP_STATUS__:")
        code = tail.strip()[:3]
    return f"HTTP {code}\n{body[:1024]}"


@mcp.tool()
async def host_process_status(match: str) -> str:
    """`ps -eo pid,cmd | grep <match>` — returns matching lines (minus the grep itself)
    or 'NONE' if no process matches."""
    rc, out = await run_host(
        f"ps -eo pid,cmd | grep -F {match!r} | grep -v grep",
        cwd=os.environ.get("HOME", "/tmp"), actor="mcp_host", timeout=5,
    )
    if rc != 0 or not out.strip():
        return "NONE"
    return out[:2000]


@mcp.tool()
async def host_tail_log(path: str, lines: int = 100) -> str:
    """Tail a log file. Path must be inside a project directory under
    host.projects_base (enforced by host_guard's cwd check)."""
    parent = os.path.dirname(path) or "/"
    rc, out = await run_host(
        f"tail -n {int(lines)} {path}",
        cwd=parent, actor="mcp_host", timeout=10,
    )
    if rc != 0:
        return f"FAILED\n{out[-500:]}"
    return out[-8000:]


@mcp.tool()
async def host_start_app(project_dir: str, start_command: str) -> str:
    """Start the app detached (nohup + setsid) so it keeps running after the
    agent's invocation ends. Returns the spawned pid + first ~2s of output."""
    log_path = os.path.join(project_dir, ".openclow-start.log")
    spawn = (
        f"nohup setsid sh -c {start_command!r} "
        f"> {log_path} 2>&1 < /dev/null & echo PID=$!"
    )
    rc, out = await run_host(
        spawn, cwd=project_dir, actor="mcp_host", timeout=10,
    )
    if rc != 0:
        return f"FAILED to spawn\n{out[-800:]}"
    # Give it a beat, then tail the fresh log
    await asyncio.sleep(2)
    _, tail = await run_host(
        f"tail -n 40 {log_path}",
        cwd=project_dir, actor="mcp_host", timeout=5,
    )
    return f"OK {out.strip()[-200:]}\n--- early output ---\n{tail[-2000:]}"


@mcp.tool()
async def host_stop_app(project_dir: str, stop_command: str = "") -> str:
    """Run the configured stop command. If empty, tries `pm2 stop .` then
    falls back to killing processes matching the project dir."""
    if stop_command:
        rc, out = await run_host(
            stop_command, cwd=project_dir, actor="mcp_host", timeout=30,
        )
        return f"{'OK' if rc == 0 else 'FAILED'} rc={rc}\n{out[-1500:]}"
    # Fallback: pm2 stop ecosystem in project dir (no-op if pm2 not installed)
    rc, out = await run_host(
        "pm2 stop ecosystem.config.js 2>/dev/null || pm2 stop .",
        cwd=project_dir, actor="mcp_host", timeout=15,
    )
    return f"{'OK' if rc == 0 else 'NOTE'} rc={rc}\n{out[-1000:]}"


@mcp.tool()
async def host_service_status(unit: str) -> str:
    """Query systemctl or pm2 for a named service. Auto-detects which one
    recognizes the unit."""
    # Try pm2 first (user-mode, no sudo needed)
    rc, out = await run_host(
        f"pm2 describe {unit} 2>&1 | head -40",
        cwd=os.environ.get("HOME", "/tmp"), actor="mcp_host", timeout=5,
    )
    if rc == 0 and "online" in out.lower():
        return f"pm2 {out[:1500]}"
    rc2, out2 = await run_host(
        f"systemctl status {unit} --no-pager --lines 20 2>&1",
        cwd=os.environ.get("HOME", "/tmp"), actor="mcp_host", timeout=5,
    )
    if out2.strip():
        return f"systemd\n{out2[:2000]}"
    return f"UNKNOWN\npm2:\n{out[:500]}\nsystemd:\n{out2[:500]}"


if __name__ == "__main__":
    mcp.run()
