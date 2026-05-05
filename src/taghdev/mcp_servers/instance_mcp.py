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
from typing import Any, Iterable

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Argv — parsed at module import. A pinned factory (claude.py:_mcp_instance)
# is the only intended caller; both args are required in production use.
# Defaults exist so `python -m taghdev.mcp_servers.instance_mcp` can still
# start in a shell for local diagnostics.
# ---------------------------------------------------------------------------


def _parse_argv(argv: list[str]) -> tuple[str, frozenset[str], int | None]:
    parser = argparse.ArgumentParser(
        prog="taghdev.mcp_servers.instance_mcp",
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
    parser.add_argument(
        "--chat-session-id",
        required=False,
        default="",
        help="Chat session ID for this MCP — pins the lifecycle tools "
             "(provision_now/instance_status/terminate_now) to one chat.",
    )
    ns, _ = parser.parse_known_args(argv)
    allowlist = frozenset(s.strip() for s in ns.allowed_services.split(",") if s.strip())
    csid: int | None
    try:
        csid = int(ns.chat_session_id) if ns.chat_session_id else None
    except ValueError:
        csid = None
    return ns.compose_project, allowlist, csid


_COMPOSE_PROJECT, _ALLOWED_SERVICES, _CHAT_SESSION_ID = _parse_argv(sys.argv[1:])

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


# ---------------------------------------------------------------------------
# Lifecycle tools — let the LLM own provision/status/terminate as a senior
# engineer would. All three resolve the active instance from the pinned
# `--chat-session-id`; none take an `instance_*` / `chat_*` argument
# (Principle III). DB access is via `async_session()` inheriting the
# worker's env vars in this subprocess.
# ---------------------------------------------------------------------------


async def _resolve_instance() -> tuple[Any, Any] | str:  # type: ignore[name-defined]
    """Look up the chat's active Instance row.

    Returns ``(instance, session_factory)`` on hit, or an error string
    on miss/refusal. ``session_factory`` is returned so callers that need
    a fresh session for follow-up writes don't reopen an import.
    """
    if _CHAT_SESSION_ID is None:
        return "REFUSED: instance_mcp started without --chat-session-id."

    from taghdev.models import async_session  # type: ignore
    from taghdev.models.instance import Instance  # type: ignore
    from taghdev.services.instance_service import ACTIVE_STATUSES  # type: ignore
    from sqlalchemy import select  # type: ignore

    async with async_session() as session:
        result = await session.execute(
            select(Instance).where(
                Instance.chat_session_id == _CHAT_SESSION_ID,
                Instance.status.in_(ACTIVE_STATUSES),
            )
        )
        inst = result.scalar_one_or_none()
    return inst, async_session


@mcp.tool()
async def instance_status() -> str:
    """Return the live status of THIS chat's instance.

    Read-only. Returns a multi-line key:value block. If no active
    instance exists, says so plainly so the LLM can decide whether
    to call provision_now() based on user intent.
    """
    try:
        resolved = await _resolve_instance()
    except Exception as e:
        return f"FAILED: {type(e).__name__}: {str(e)[:200]}"
    if isinstance(resolved, str):
        return resolved
    inst, _ = resolved
    if inst is None:
        return (
            "status: none\n"
            "(No instance is provisioned for this chat. "
            "Call provision_now() to bring one up.)"
        )
    web_hostname = None
    try:
        if inst.tunnels:
            web_hostname = inst.tunnels[0].web_hostname
    except Exception:
        web_hostname = None
    started = inst.started_at.isoformat() if inst.started_at else "(not yet started)"
    last_act = (
        inst.last_activity_at.isoformat() if inst.last_activity_at else "(unknown)"
    )
    return (
        f"status: {inst.status}\n"
        f"slug: {inst.slug}\n"
        f"web_hostname: {web_hostname or '(not yet assigned)'}\n"
        f"started_at: {started}\n"
        f"last_activity_at: {last_act}\n"
        f"failure_code: {inst.failure_code or '(none)'}"
    )


@mcp.tool()
async def provision_now() -> str:
    """Bring this chat's instance up. Idempotent.

    Calls InstanceService.get_or_resume(). If an active instance
    exists, returns its current state. Otherwise enqueues the provision
    job and returns immediately — provisioning runs async (~60-90s).
    Poll instance_status() for completion.
    """
    if _CHAT_SESSION_ID is None:
        return "REFUSED: instance_mcp started without --chat-session-id."
    try:
        from taghdev.services.instance_service import InstanceService  # type: ignore
        svc = InstanceService()
        inst = await svc.get_or_resume(_CHAT_SESSION_ID)
    except Exception as e:
        return f"FAILED: {type(e).__name__}: {str(e)[:200]}"
    web_hostname = None
    try:
        if inst.tunnels:
            web_hostname = inst.tunnels[0].web_hostname
    except Exception:
        web_hostname = None
    return (
        f"OK: provision in flight (or already running).\n"
        f"slug: {inst.slug}\n"
        f"status: {inst.status}\n"
        f"web_hostname: {web_hostname or '(not yet assigned)'}\n"
        f"Note: provisioning is async (~60-90s on cold boot). "
        f"Use instance_status() to poll."
    )


@mcp.tool()
async def terminate_now() -> str:
    """Tear down this chat's instance. User-triggered termination.

    Refuses if no active instance exists. Idempotent on already-
    terminating rows. Cleanup (compose down, tunnel destroy, DB row
    flip to destroyed) runs in the worker via the teardown ARQ job.
    """
    try:
        resolved = await _resolve_instance()
    except Exception as e:
        return f"FAILED: {type(e).__name__}: {str(e)[:200]}"
    if isinstance(resolved, str):
        return resolved
    inst, _ = resolved
    if inst is None:
        return "REFUSED: no active instance to terminate."
    instance_id = inst.id
    slug = inst.slug
    try:
        from taghdev.services.instance_service import InstanceService  # type: ignore
        await InstanceService().terminate(instance_id, reason="user_request")
    except Exception as e:
        return f"FAILED: {type(e).__name__}: {str(e)[:200]}"
    return f"OK: terminate enqueued. slug={slug}"


def get_tool_manifest() -> list[dict]:
    """T033 helper — list each registered tool and its parameter schema.

    Returns ``[{"name": str, "parameters": dict (JSON schema)}]``. Used
    by ``tests/unit/test_mcp_manifest.py`` to assert no tool schema
    contains an ambient-identifier argument (Principle III).
    """
    return [
        {"name": t.name, "parameters": t.parameters}
        for t in mcp._tool_manager.list_tools()
    ]


if __name__ == "__main__":
    mcp.run(transport="stdio")
