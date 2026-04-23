"""Instance MCP Server — bounded-authority compose operations for one chat's instance.

Spec: specs/001-per-chat-instances/tasks.md T038; plan.md §MCP servers.

Constitution Principle III ("bounded authority, pinned at spawn"):
  * The compose project name is fixed at process start via `--compose-project`.
  * The service allowlist is fixed at process start via `--allowed-services`.
  * No tool accepts an `instance_*`, `project_*`, `workspace_*`, or
    `container_*` argument — an LLM that tried to reach a different chat's
    instance by supplying a different name could not, because those
    arguments do not exist (T033 enforces this).
  * `cloudflared` is NEVER in the default allowlist. An operator can
    choose to pass it via `--allowed-services` for debugging, but the
    orchestrator-owned factory in providers/llm/claude.py never does.

Every tool delegates to ``docker compose -p <project> <verb> <service>``,
runs via ``asyncio.create_subprocess_exec`` with an explicit timeout
(Principle IX), and returns text on every path — never a non-zero exit
code (the Claude Agent SDK crashes on those).
"""
from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
from typing import Iterable

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Argv — parsed at module import. A pinned factory (claude.py:_mcp_instance)
# is the only intended caller; both args are required in production use.
# Defaults exist so `python -m openclow.mcp_servers.instance_mcp` can still
# start in a shell for local diagnostics.
# ---------------------------------------------------------------------------


def _parse_argv(argv: list[str]) -> tuple[str, frozenset[str]]:
    parser = argparse.ArgumentParser(
        prog="openclow.mcp_servers.instance_mcp",
        description="Per-instance compose operations, pinned to one project.",
    )
    parser.add_argument(
        "--compose-project",
        required=False,
        default="",
        help="Docker compose project name (e.g. tagh-inst-<slug>).",
    )
    parser.add_argument(
        "--allowed-services",
        required=False,
        default="app,web,node,db,redis",
        help="Comma-separated service allowlist. `cloudflared` must NOT appear.",
    )
    ns, _ = parser.parse_known_args(argv)
    allowlist = frozenset(s.strip() for s in ns.allowed_services.split(",") if s.strip())
    return ns.compose_project, allowlist


_COMPOSE_PROJECT, _ALLOWED_SERVICES = _parse_argv(sys.argv[1:])

# Refuse to start if an operator accidentally allowlists the sidecar.
# cloudflared holds the CF tunnel token; exposing it to an agent would
# let the agent restart the tunnel or exfiltrate logs containing CF edge
# metadata. Principle III forbids this for good.
if "cloudflared" in _ALLOWED_SERVICES:
    print(
        "instance_mcp: refusing to start — 'cloudflared' is in the service "
        "allowlist. Remove it from --allowed-services.",
        file=sys.stderr,
    )
    sys.exit(2)


mcp = FastMCP("instance")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_EXEC_TIMEOUT_S = 120
_META_TIMEOUT_S = 30
_LOG_TAIL_DEFAULT = 200
_LOG_TAIL_MAX = 2000


def _reject_service(service: str) -> str:
    return (
        f"REFUSED: service {service!r} is not in the allowlist "
        f"{sorted(_ALLOWED_SERVICES)}. Use one of the allowed services."
    )


def _ensure_allowed(service: str) -> str | None:
    """Return an error string if `service` is not allowed, else None."""
    if service not in _ALLOWED_SERVICES:
        return _reject_service(service)
    return None


async def _run_compose(
    *verb: str,
    timeout: int,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Shell out to ``docker compose -p <project> <verb...>``.

    Always returns text. Never raises. Never exposes the CF tunnel token
    because this module never reads it — compose inherits the orchestrator
    worker's env, which is separate from this subprocess's env.
    """
    if not _COMPOSE_PROJECT:
        return "REFUSED: instance_mcp started without --compose-project."

    # No docker_guard wrapping: this MCP is already a scoped boundary.
    # The subprocess timeout is the enforcement.
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-p", _COMPOSE_PROJECT, *verb,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        return f"FAILED: docker compose {' '.join(verb)} timed out after {timeout}s"
    except FileNotFoundError:
        return "FAILED: docker CLI not available to this process"
    except Exception as e:
        return f"FAILED: {type(e).__name__}: {str(e)[:200]}"

    output = (stdout or b"").decode(errors="replace")
    errtxt = (stderr or b"").decode(errors="replace")
    if proc.returncode != 0:
        return (
            f"FAILED (exit {proc.returncode}):\n"
            f"{(output + errtxt).strip()[-4000:]}"
        )
    return (output or errtxt or "(no output)")[-4000:]


# ---------------------------------------------------------------------------
# Tools — every argument name is free of `instance`, `project`, `workspace`,
# or `container` per T033 enforcement.
# ---------------------------------------------------------------------------


@mcp.tool()
async def instance_exec(service: str, cmd: str) -> str:
    """Run a shell command inside one of the allowed compose services.

    Returns stdout+stderr concatenated. 120s timeout. `service` must be in
    the allowlist chosen at spawn time; `cloudflared` is never allowed.
    """
    err = _ensure_allowed(service)
    if err:
        return err
    # Split the command into argv so compose exec does not invoke a shell.
    # Agents that want shell features can do `sh -c "..."` explicitly and
    # wear the risk; this keeps the default path free of injection.
    try:
        cmd_argv = shlex.split(cmd)
    except ValueError as e:
        return f"FAILED: could not parse cmd: {e}"
    if not cmd_argv:
        return "FAILED: empty cmd"
    return await _run_compose(
        "exec", "-T", service, *cmd_argv, timeout=_EXEC_TIMEOUT_S
    )


@mcp.tool()
async def instance_logs(service: str, tail: int = _LOG_TAIL_DEFAULT) -> str:
    """Return recent logs from one service. `tail` is clamped to 2000 lines."""
    err = _ensure_allowed(service)
    if err:
        return err
    n = max(1, min(int(tail or _LOG_TAIL_DEFAULT), _LOG_TAIL_MAX))
    return await _run_compose(
        "logs", "--no-color", "--tail", str(n), service,
        timeout=_META_TIMEOUT_S,
    )


@mcp.tool()
async def instance_restart(service: str) -> str:
    """Restart one allowed service. The service keeps its compose config."""
    err = _ensure_allowed(service)
    if err:
        return err
    return await _run_compose("restart", service, timeout=_META_TIMEOUT_S)


@mcp.tool()
async def instance_ps() -> str:
    """List the running containers for this instance."""
    return await _run_compose(
        "ps", "--format", "table", timeout=_META_TIMEOUT_S
    )


@mcp.tool()
async def instance_health() -> str:
    """Return the docker-reported health of each allowed service.

    Runs ``docker compose ps --format json`` once and filters to the
    allowlist, so a sidecar added out-of-band cannot appear in the
    output.
    """
    raw = await _run_compose(
        "ps", "--format", "json", timeout=_META_TIMEOUT_S
    )
    if raw.startswith("FAILED") or raw.startswith("REFUSED"):
        return raw
    # compose ps --format json emits one JSON object per line.
    import json
    lines: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        svc = obj.get("Service") or obj.get("Name") or ""
        if svc not in _ALLOWED_SERVICES:
            continue
        health = obj.get("Health") or obj.get("State") or "unknown"
        status = obj.get("Status") or ""
        lines.append(f"{svc}: {health} ({status})")
    return "\n".join(lines) or "(no allowed services reporting)"


if __name__ == "__main__":
    mcp.run(transport="stdio")
