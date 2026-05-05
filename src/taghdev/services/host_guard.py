"""Host command guard — allowlist/blocklist for agent shell operations on the VPS host.

Mirrors docker_guard. Used by the host_mcp MCP server and project_exec helper so
the agent can pull code, install deps, start services, tail logs, etc. inside a
project directory without being able to blast the host.

Confines writes to under settings.host_projects_base (realpath-resolved) and
routes every call through the same audit_service log_action/log_blocked helpers
the Docker path uses.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from typing import Optional

from taghdev.services.audit_service import log_action, log_blocked
from taghdev.utils.logging import get_logger

log = get_logger()


# ─────────────────────────────────────────────
# Allowlist — first token of the command must be one of these
# ─────────────────────────────────────────────
ALLOWED_COMMAND_PREFIXES = {
    # version control
    "git",
    # python
    "python", "python3", "pip", "pip3", "uvicorn", "gunicorn", "pytest",
    # node
    "node", "npm", "yarn", "pnpm", "npx",
    # php
    "php", "composer", "artisan",
    # ruby
    "ruby", "bundle", "rails", "rake",
    # go / java / rust
    "go", "mvn", "gradle", "cargo",
    # build tools
    "make",
    # process managers
    "pm2", "systemctl", "service", "supervisorctl", "journalctl",
    # network / diagnostics
    "curl", "wget", "ss", "lsof", "netstat", "ping",
    # filesystem read-only
    "ls", "cat", "head", "tail", "grep", "find", "pwd", "which", "test", "file", "stat",
    # trivial helpers
    "echo", "true", "false", "env", "date",
    # archives (read-mostly)
    "tar", "unzip", "zip",
    # database CLIs (read-only shape; safety relies on blocklist below)
    "psql", "mysql", "sqlite3", "redis-cli",
}

# ─────────────────────────────────────────────
# Blocklist — regexes that reject the command outright
# ─────────────────────────────────────────────
BLOCKED_PATTERNS = [
    # destructive filesystem
    r"\brm\s+(-\w*\s+)?-[rRf]+\s+/",       # rm -rf /, rm -rf /etc, etc.
    r"\bdd\s+if=",
    r"\bmkfs\b",
    r"\bshred\b",
    # reboots / shutdowns
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\binit\s+0\b",
    r"\binit\s+6\b",
    # privilege escalation
    r"\bsudo\s+su\b",
    r"\bsu\s+-\b",
    r"\bsudo\s+-s\b",
    r"\bchmod\s+[-+]?[0-7]*s",             # setuid bit
    # fork-bomb / remote-execution patterns
    r":\(\)\s*\{",                          # :(){ :|:& };:
    r"curl[^|]+\|\s*(sh|bash)\b",
    r"wget[^|]+\|\s*(sh|bash)\b",
    # writes to system paths
    r">\s*/etc/",
    r">\s*/boot/",
    r">\s*/root/",
    r">\s*/dev/sd",
    r"\bmv\b[^#]*\s+/etc/",
    r"\bmv\b[^#]*\s+/boot/",
    # destructive DB commands
    r"\bDROP\s+DATABASE\b",
    r"\bTRUNCATE\s+DATABASE\b",
]
_BLOCKED_RE = [re.compile(p, re.IGNORECASE) for p in BLOCKED_PATTERNS]


def _first_token(cmd: str) -> str:
    """Return the first real command token (skips env var assignments like FOO=bar)."""
    try:
        tokens = shlex.split(cmd, comments=True, posix=True)
    except ValueError:
        tokens = cmd.split()
    for t in tokens:
        if "=" in t and t.split("=", 1)[0].isidentifier():
            continue  # env assignment prefix
        return t.lstrip("./").split("/")[-1]
    return ""


def is_allowed(cmd: str) -> tuple[bool, str]:
    """Check if a command is allowed. Returns (allowed, reason)."""
    for pattern in _BLOCKED_RE:
        if pattern.search(cmd):
            return False, f"Blocked pattern: {pattern.pattern}"

    first = _first_token(cmd)
    if not first:
        return False, "Empty command"

    # allow pipes & simple shell composition by checking the FIRST command only;
    # blocklist above catches the dangerous pipe shapes (curl ... | sh).
    first = first.split("|")[0].strip()
    if first in ALLOWED_COMMAND_PREFIXES:
        return True, "allowed"

    return False, f"Command '{first}' not in allowlist"


def _is_within(base: str, path: str) -> bool:
    """Allow the path if it sits under base — by either lexical OR realpath
    comparison. The lexical pass handles the common staging layout where
    /srv/projects/<name> is a symlink to /home/web/vhosts/<name>/htdocs/live;
    realpath alone would reject that because the target is outside base. The
    realpath pass is kept as a backstop for the case where base itself is a
    symlink. Sandbox-safe: both forms must START with base, no '..' escape.
    """
    try:
        base_norm = os.path.normpath(base)
        path_norm = os.path.normpath(path)
        if path_norm == base_norm or path_norm.startswith(base_norm + os.sep):
            return True
        base_r = os.path.realpath(base)
        path_r = os.path.realpath(path)
        return path_r == base_r or path_r.startswith(base_r + os.sep)
    except Exception:
        return False


async def run_host(
    command: str,
    *,
    cwd: str,
    timeout: int = 120,
    actor: str = "system",
    project_name: str | None = None,
    project_id: int | None = None,
    env: Optional[dict] = None,
    stream_channel: Optional[str] = None,
) -> tuple[int, str]:
    """Run a shell command inside a project directory on the host.

    - Validates cwd is under the configured host projects base (realpath).
    - Applies allowlist + blocklist.
    - Streams stdout/stderr line-by-line into a Redis pub/sub channel when
      `stream_channel` is provided (used for live tool-output in the UI).
    - Audit-logs every action, blocked or not.
    Returns (returncode, truncated_output).
    """
    from taghdev.services.config_service import get_host_setting

    projects_base = await get_host_setting("projects_base")
    if not projects_base:
        return -1, "BLOCKED: host.projects_base is not configured"

    if not _is_within(projects_base, cwd):
        await log_blocked(
            actor=actor, action="host", command=command,
            reason=f"cwd {cwd} is outside {projects_base}", project_name=project_name,
        )
        return -1, f"BLOCKED: cwd {cwd!r} is outside configured host.projects_base"

    allowed, reason = is_allowed(command)
    if not allowed:
        await log_blocked(
            actor=actor, action="host", command=command,
            reason=reason, project_name=project_name,
        )
        log.warning("host_guard.blocked", command=command[:200], reason=reason)
        return -1, f"BLOCKED: {reason}"

    merged_env = os.environ.copy()
    if env:
        merged_env.update({k: str(v) for k, v in env.items()})

    output_chunks: list[str] = []

    async def _drain(stream: asyncio.StreamReader, redis_pub):
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode(errors="replace")
            output_chunks.append(text)
            if redis_pub and stream_channel:
                try:
                    await redis_pub.publish(stream_channel, json.dumps({
                        "type": "tool_output",
                        "chunk": text[:4096],
                        "final": False,
                    }))
                except Exception:
                    pass

    redis_pub = None
    if stream_channel:
        try:
            import redis.asyncio as aioredis
            from taghdev.settings import settings as _s
            redis_pub = aioredis.from_url(_s.redis_url)
        except Exception:
            redis_pub = None

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            env=merged_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(_drain(proc.stdout, redis_pub), timeout=timeout)
            rc = await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            output_chunks.append(f"\n[TIMEOUT after {timeout}s]")
            rc = -1
    except Exception as e:
        output_chunks.append(str(e))
        rc = -1

    output = "".join(output_chunks)[-16000:].strip()

    if redis_pub and stream_channel:
        try:
            await redis_pub.publish(stream_channel, json.dumps({
                "type": "tool_output", "chunk": "", "final": True, "rc": rc,
            }))
        except Exception:
            pass
        try:
            await redis_pub.aclose()
        except Exception:
            pass

    await log_action(
        actor=actor,
        action="host",
        command=command,
        workspace=cwd,
        project_name=project_name,
        exit_code=rc,
        output_summary=output[:2000],
        metadata={"project_id": project_id, "ts": time.time()},
    )
    return rc, output
