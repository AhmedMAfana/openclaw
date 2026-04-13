"""Docker command guard — allowlist/blocklist for agent Docker operations.

Prevents Claude agents from running destructive Docker commands like:
  docker rm -f $(docker ps -q)
  docker system prune
  docker rmi --force

Allows safe operations like:
  docker ps, docker logs, docker compose up, docker inspect, docker restart
"""
import asyncio
import os
import re

import json
import socket

from openclow.services.audit_service import log_action, log_blocked
from openclow.utils.logging import get_logger

log = get_logger()

# ─────────────────────────────────────────────
# Auto-detect host workspace path (Docker-in-Docker)
# ─────────────────────────────────────────────

_host_workspace_path: str | None = None
_host_detect_done = False


async def _detect_host_workspace_path() -> str | None:
    """Auto-detect the real host path for /workspaces.

    The worker runs inside a container but uses the HOST Docker socket.
    Project docker-compose files have relative volume mounts (./:/var/www).
    These must resolve on the HOST filesystem, not the container filesystem.

    We inspect our own container to find where /workspaces is mounted from.
    Handles Docker Desktop Mac's /host_mnt/ prefix automatically.
    """
    global _host_workspace_path, _host_detect_done
    if _host_detect_done:
        return _host_workspace_path

    _host_detect_done = True

    try:
        container_id = socket.gethostname()
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", container_id,
            "--format", '{{json .Mounts}}',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            log.warning("docker_guard.self_inspect_failed")
            return None

        mounts = json.loads(stdout.decode())
        for mount in mounts:
            if mount.get("Destination") == "/workspaces":
                source = mount.get("Source", "")
                # Docker Desktop Mac adds /host_mnt/ prefix — strip it
                if source.startswith("/host_mnt/"):
                    source = source[len("/host_mnt"):]  # keeps the leading /
                _host_workspace_path = source
                log.info("docker_guard.host_path_detected", path=source)
                return source

        log.warning("docker_guard.no_workspace_mount")
        return None
    except Exception as e:
        log.warning("docker_guard.detect_failed", error=str(e))
        return None

# ─────────────────────────────────────────────
# Allowlist: these Docker subcommands are safe
# ─────────────────────────────────────────────

ALLOWED_DOCKER_COMMANDS = {
    # Read-only / inspection
    "ps", "logs", "inspect", "stats", "top", "port",
    "images", "image ls", "image inspect",
    "network ls", "network inspect",
    "volume ls", "volume inspect",
    "info", "version",

    # Compose (project-scoped)
    "compose up", "compose down", "compose ps", "compose logs",
    "compose build", "compose restart", "compose stop",
    "compose start", "compose exec", "compose pull",
    "compose config",

    # Container lifecycle (single container)
    "restart", "start", "stop",
    "exec",
    "cp",

    # Build
    "build",
}

# ─────────────────────────────────────────────
# Blocklist: NEVER allow these, even if they
# match an allowed prefix
# ─────────────────────────────────────────────

BLOCKED_PATTERNS = [
    # Nuclear options
    r"docker\s+system\s+prune",
    r"docker\s+container\s+prune",
    r"docker\s+image\s+prune",
    r"docker\s+volume\s+prune",
    r"docker\s+network\s+prune",
    r"docker\s+builder\s+prune",

    # Force remove containers/images/volumes
    r"docker\s+rm\b",
    r"docker\s+rmi\b",
    r"docker\s+volume\s+rm\b",
    r"docker\s+network\s+rm\b",
    r"docker\s+container\s+rm\b",

    # Kill all / remove all
    r"docker\s+kill\b",
    r"\$\(docker\s+ps",   # subshell expansion like docker rm $(docker ps -q)

    # Compose remove volumes (data loss)
    r"docker\s+compose.*\s-v\b(?!ersion)",  # -v flag on compose down = remove volumes
    # but NOT -version

    # Escape / privilege escalation
    r"docker\s+run\b.*--privileged",
    r"docker\s+run\b.*--pid\s*=\s*host",
    r"docker\s+run\b.*--net\s*=\s*host",
    r"docker\s+run\b.*-v\s*/:/",  # mount host root

    # Direct socket manipulation
    r"docker\s+run\b.*docker\.sock",
]

# Compiled for performance
_BLOCKED_RE = [re.compile(p, re.IGNORECASE) for p in BLOCKED_PATTERNS]


def _extract_docker_subcommand(cmd: str) -> str | None:
    """Extract the Docker subcommand from a command string.

    'docker compose -f foo.yml up -d --build' → 'compose up'
    'docker logs my-container' → 'logs'
    'docker ps -a --filter name=foo' → 'ps'
    """
    # Strip leading env vars, sudo, etc.
    cmd = cmd.strip()
    parts = cmd.split()

    # Find 'docker' in the command
    try:
        idx = parts.index("docker")
    except ValueError:
        return None

    remaining = parts[idx + 1:]
    if not remaining:
        return None

    # Skip flags that come before the subcommand (e.g., docker -H ...)
    while remaining and remaining[0].startswith("-"):
        remaining = remaining[1:]
        if remaining and not remaining[0].startswith("-"):
            remaining = remaining[1:]  # skip flag value

    if not remaining:
        return None

    subcmd = remaining[0]

    # For compose, include the compose subcommand
    if subcmd == "compose":
        # Skip compose flags (-f, -p, etc.)
        compose_rest = remaining[1:]
        while compose_rest and compose_rest[0].startswith("-"):
            compose_rest = compose_rest[1:]
            if compose_rest and not compose_rest[0].startswith("-"):
                compose_rest = compose_rest[1:]
        if compose_rest:
            return f"compose {compose_rest[0]}"
        return "compose"

    # For multi-word commands like "image ls"
    if subcmd in ("image", "container", "network", "volume", "builder"):
        if len(remaining) > 1 and not remaining[1].startswith("-"):
            return f"{subcmd} {remaining[1]}"

    return subcmd


def is_allowed(cmd: str) -> tuple[bool, str]:
    """Check if a Docker command is allowed.

    Returns (allowed, reason).
    """
    # Check blocklist first — these override everything
    for pattern in _BLOCKED_RE:
        if pattern.search(cmd):
            return False, f"Blocked pattern: {pattern.pattern}"

    # Extract and check subcommand
    subcmd = _extract_docker_subcommand(cmd)
    if subcmd is None:
        return False, "Could not parse Docker subcommand"

    if subcmd in ALLOWED_DOCKER_COMMANDS:
        return True, "allowed"

    return False, f"Docker subcommand '{subcmd}' not in allowlist"


async def run_docker(
    *args: str,
    actor: str = "system",
    project_name: str | None = None,
    project_id: int | None = None,
    workspace: str | None = None,
    cwd: str | None = None,
    timeout: int = 300,
    metadata: dict | None = None,
) -> tuple[int, str]:
    """Safe Docker command executor with allowlist enforcement.

    Use this instead of raw subprocess calls for all Docker operations
    that Claude agents trigger.

    Returns (returncode, output).
    """
    cmd_str = " ".join(args)

    # Auto-extend timeout for slow compose operations (build, up with build)
    if "compose" in cmd_str and ("build" in cmd_str or ("up" in cmd_str and "--build" in cmd_str)):
        timeout = max(timeout, 600)

    # Check allowlist
    allowed, reason = is_allowed(cmd_str)

    if not allowed:
        await log_blocked(
            actor=actor, action="docker", command=cmd_str,
            reason=reason, project_name=project_name,
        )
        log.warning("docker_guard.blocked", command=cmd_str[:200], reason=reason)
        return -1, f"BLOCKED: {reason}"

    # Run the command
    env = {**os.environ}
    final_args = list(args)

    # Docker-in-Docker fix: inject --project-directory for compose commands
    # so relative volume mounts resolve on the HOST filesystem, not in the worker container.
    # Also inject --env-file so compose finds the .env from the container path.
    if cwd and "compose" in cmd_str:
        from openclow.settings import settings
        host_path = await _detect_host_workspace_path()
        if host_path and cwd.startswith(settings.workspace_base_path):
            host_cwd = cwd.replace(settings.workspace_base_path, host_path, 1)
            try:
                compose_idx = final_args.index("compose")
                insert_at = compose_idx + 1
                # --project-directory: resolve relative volume paths from host
                final_args.insert(insert_at, "--project-directory")
                final_args.insert(insert_at + 1, host_cwd)
                # --env-file: .env is inside the container at cwd, compose can read it
                env_file = os.path.join(cwd, ".env")
                if os.path.exists(env_file):
                    final_args.insert(insert_at + 2, "--env-file")
                    final_args.insert(insert_at + 3, env_file)
                log.debug("docker_guard.host_path_injected", host_cwd=host_cwd)
            except ValueError:
                pass

    # Port isolation: inject unique port env vars so projects don't conflict
    if project_id and "compose" in cmd_str:
        from openclow.services.port_allocator import get_port_env_vars
        port_vars = get_port_env_vars(project_id)
        env.update(port_vars)
        log.debug("docker_guard.ports_injected", project_id=project_id,
                  app=port_vars.get("APP_PORT"), db=port_vars.get("FORWARD_DB_PORT"))

    try:
        proc = await asyncio.create_subprocess_exec(
            *final_args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = (stdout.decode() + stderr.decode()).strip()[-16000:]
        rc = proc.returncode
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        output = f"TIMEOUT after {timeout}s"
        rc = -1
    except Exception as e:
        output = str(e)
        rc = -1

    # Audit log
    await log_action(
        actor=actor,
        action="docker",
        command=cmd_str,
        workspace=workspace,
        project_name=project_name,
        exit_code=rc,
        output_summary=output[:2000],
        metadata=metadata,
    )

    return rc, output


async def run_docker_compose(
    *compose_args: str,
    compose_file: str = "docker-compose.yml",
    compose_project: str = "",
    actor: str = "system",
    project_name: str | None = None,
    workspace: str | None = None,
    cwd: str | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    """Convenience wrapper for docker compose commands.

    Usage:
        rc, output = await run_docker_compose(
            "up", "-d", "--build",
            compose_file="docker-compose.yml",
            compose_project="openclow-trade-bot",
            actor="bootstrap",
            cwd="/workspaces/_cache/trade-bot",
        )
    """
    args = ["docker", "compose"]
    if compose_file:
        args.extend(["-f", compose_file])
    if compose_project:
        args.extend(["-p", compose_project])
    args.extend(compose_args)

    return await run_docker(
        *args,
        actor=actor,
        project_name=project_name,
        workspace=workspace,
        cwd=cwd,
        timeout=timeout,
    )
