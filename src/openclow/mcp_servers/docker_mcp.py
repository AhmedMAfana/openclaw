"""Docker MCP Server — gives agents direct access to Docker operations.

All commands go through docker_guard for allowlist enforcement and audit logging.
Agents cannot run destructive Docker commands (rm, prune, rmi, etc.).
"""
import asyncio
import json
import os
import shlex

from mcp.server.fastmcp import FastMCP

from openclow.services.docker_guard import run_docker
from openclow.utils.docker_path import get_docker_env

mcp = FastMCP("docker")


@mcp.tool()
async def list_containers(project_filter: str = "") -> str:
    """List all running Docker containers. Optionally filter by project name."""
    args = ["docker", "ps", "--format", "json"]
    if project_filter:
        args += ["--filter", f"name={project_filter}"]

    rc, output = await run_docker(*args, actor="mcp_docker")
    if rc != 0 or not output.strip():
        return "No containers running"

    lines = []
    for line in output.split("\n"):
        try:
            data = json.loads(line)
            lines.append(f"  {data.get('Names', '?')}: {data.get('State', '?')} ({data.get('Status', '?')}) — {data.get('Image', '?')}")
        except json.JSONDecodeError:
            continue
    return "\n".join(lines) or "No containers found"


@mcp.tool()
async def container_logs(container_name: str, tail: int = 50) -> str:
    """Get recent logs from a Docker container."""
    rc, output = await run_docker(
        "docker", "logs", container_name, "--tail", str(tail),
        actor="mcp_docker",
    )
    return output[-5000:]


@mcp.tool()
async def container_health(container_name: str) -> str:
    """Check health status of a specific container."""
    rc, output = await run_docker(
        "docker", "inspect", container_name,
        "--format", "{{.State.Status}} {{.State.Health.Status}}",
        actor="mcp_docker",
    )
    return output.strip()


@mcp.tool()
async def restart_container(container_name: str) -> str:
    """Restart a Docker container."""
    rc, output = await run_docker(
        "docker", "restart", container_name,
        actor="mcp_docker",
    )
    if rc == 0:
        return f"Container {container_name} restarted successfully"
    return f"Failed to restart: {output}"


@mcp.tool()
async def docker_exec(container_name: str, command: str) -> str:
    """Execute a command inside a running Docker container. Returns exit code and output.

    Timeout: 60s. For long-running commands (migrations, npm build), use compose_build instead.
    """
    cmd_parts = shlex.split(command)
    rc, output = await run_docker(
        "docker", "exec", container_name, *cmd_parts,
        actor="mcp_docker",
        timeout=60,
    )
    if rc != 0:
        return f"FAILED (exit code {rc}):\n{output[-5000:]}"
    return output[-5000:]


@mcp.tool()
async def compose_build(compose_file: str, project_name: str, working_dir: str) -> str:
    """Build Docker images for a Compose stack.

    Non-blocking after 30s: starts the build, waits up to 30s for early failures
    (bad Dockerfile, auth errors, etc.). If still running after 30s, returns BUILDING
    with recent output. Call compose_build_status(project_name) to poll for completion.

    After DONE: call compose_up() to start containers.
    """
    log_path = f"/tmp/docker_build_{project_name}.log"

    from openclow.utils.docker_path import get_docker_bin
    try:
        docker_bin = get_docker_bin()
    except FileNotFoundError as e:
        return f"FAILED — Docker binary not found: {e}"

    try:
        log_f = open(log_path, "w")
        proc = await asyncio.create_subprocess_exec(
            docker_bin, "compose", "-f", compose_file, "-p", project_name, "build",
            "--progress", "plain",
            stdin=asyncio.subprocess.DEVNULL,   # never inherit MCP server's stdin
            stdout=log_f,
            stderr=asyncio.subprocess.STDOUT,
            cwd=working_dir,
            env=get_docker_env(),
        )
    except Exception as e:
        return f"FAILED to start build: {e}"

    # Write PID so compose_build_status can check if still running
    try:
        open(f"/tmp/docker_build_{project_name}.pid", "w").write(str(proc.pid))
    except Exception:
        pass

    # Track exit code in background — written to .rc file when process finishes.
    # compose_build_status reads this instead of text-matching output for reliable
    # pass/fail detection (output may contain the word "error" in warnings even on success).
    async def _track_build_exit(p: asyncio.subprocess.Process, name: str):
        try:
            await p.wait()
            open(f"/tmp/docker_build_{name}.rc", "w").write(str(p.returncode or 0))
        except Exception:
            pass
    _t = asyncio.create_task(_track_build_exit(proc, project_name))
    _t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    try:
        # Wait up to 30s — enough to catch auth errors, bad Dockerfiles, immediate failures
        await asyncio.wait_for(proc.wait(), timeout=30)
        log_f.close()
        output = open(log_path).read()[-5000:].strip() if os.path.exists(log_path) else ""
        if proc.returncode == 0:
            return f"DONE (build succeeded):\n{output}"
        return f"FAILED (exit code {proc.returncode}):\n{output}"
    except asyncio.TimeoutError:
        # Still building — return partial output so MCP connection stays alive.
        # Agent should poll compose_build_status() every 30s.
        log_f.close()
        partial = ""
        try:
            partial = open(log_path).read()[-800:].strip()
        except Exception:
            pass
        return (
            f"BUILDING — docker build is running in the background (builds take 2-15min on ARM).\n"
            f"Call compose_build_status('{project_name}') every 30s to check progress.\n"
            f"Recent output:\n{partial}"
        )


@mcp.tool()
async def compose_build_status(project_name: str) -> str:
    """Check the status of a background compose_build() call.

    Returns:
      "DONE"      — build finished successfully, call compose_up() now
      "FAILED"    — build failed, check the output
      "BUILDING"  — still in progress, call again in 30s
    """
    log_path = f"/tmp/docker_build_{project_name}.log"
    pid_path = f"/tmp/docker_build_{project_name}.pid"

    if not os.path.exists(pid_path):
        return "No build in progress for this project (or it already finished)."

    try:
        pid = int(open(pid_path).read().strip())
        # Check if process is still alive
        import signal
        try:
            os.kill(pid, 0)  # signal 0 = check existence, no actual signal sent
            still_running = True
        except ProcessLookupError:
            still_running = False
    except Exception:
        still_running = False

    output = ""
    if os.path.exists(log_path):
        output = open(log_path).read()[-3000:].strip()

    if still_running:
        return f"BUILDING — still in progress. Call again in 30s.\nRecent output:\n{output[-500:]}"

    # Process exited — use exit code (.rc file) for reliable pass/fail.
    # Text matching is unreliable: output can contain "error" in warnings even on success.
    os.unlink(pid_path)
    rc_path = f"/tmp/docker_build_{project_name}.rc"
    exit_code: int | None = None
    if os.path.exists(rc_path):
        try:
            exit_code = int(open(rc_path).read().strip())
            os.unlink(rc_path)
        except Exception:
            pass

    if exit_code == 0:
        return f"DONE — build complete. Call compose_up() now.\nOutput:\n{output}"
    if exit_code is not None:
        return f"FAILED (exit code {exit_code}):\n{output}"
    # Fallback: .rc file not written yet (race) — use text matching
    if "error" in output.lower() and "successfully" not in output.lower():
        return f"FAILED — build exited with errors:\n{output}"
    return f"DONE — build complete. Call compose_up() now.\nOutput:\n{output}"


@mcp.tool()
async def compose_up(compose_file: str, project_name: str, working_dir: str) -> str:
    """Start a Docker Compose stack (containers only, no build).

    Non-blocking: launches 'docker compose up -d' in the background.
    Waits up to 20s for early failure detection (image missing, port conflict, etc.).
    If no early failure, returns STARTED — call compose_ps() to watch containers come up.

    Call compose_build() first if images don't exist yet.
    """
    from openclow.services.docker_guard import is_allowed, get_docker_env, _detect_host_workspace_path
    from openclow.utils.docker_path import get_docker_bin
    from openclow.settings import settings as _s

    log_path = f"/tmp/compose_up_{project_name}.log"
    env = get_docker_env()

    # Resolve absolute docker binary — don't rely on PATH lookup in MCP subprocess env
    try:
        docker_bin = get_docker_bin()
    except FileNotFoundError as e:
        return f"FAILED — Docker binary not found: {e}"

    # Build the command with --project-directory (host path resolution for Docker-in-Docker)
    up_args = [docker_bin, "compose", "-f", compose_file, "-p", project_name]

    host_path = await _detect_host_workspace_path()
    if host_path and working_dir.startswith(_s.workspace_base_path):
        host_cwd = working_dir.replace(_s.workspace_base_path, host_path, 1)
        up_args += ["--project-directory", host_cwd]
        env_file = os.path.join(working_dir, ".env")
        if os.path.exists(env_file):
            up_args += ["--env-file", env_file]

    up_args += ["up", "-d"]

    cmd_str = " ".join(up_args)
    allowed, reason = is_allowed(cmd_str)
    if not allowed:
        return f"BLOCKED: {reason}"

    try:
        log_f = open(log_path, "w")
        proc = await asyncio.create_subprocess_exec(
            *up_args,
            cwd=working_dir,
            stdin=asyncio.subprocess.DEVNULL,   # never inherit MCP server's stdin
            stdout=log_f,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except Exception as e:
        return f"FAILED to start: {e}"

    # Wait up to 20s — enough to catch immediate failures (image not found, port conflict).
    # docker compose up -d exits immediately when successful (detached mode).
    try:
        await asyncio.wait_for(proc.wait(), timeout=20)
        log_f.close()
        output = open(log_path).read()[-3000:].strip()
        if proc.returncode == 0:
            return f"SUCCESS:\n{output}"
        # Classify the failure for actionable agent decisions
        low = output.lower()
        if any(k in low for k in ("pull access denied", "no such image", "manifest unknown", "not found")):
            return (
                f"FAILED — image not found. Call compose_build('{compose_file}', "
                f"'{project_name}', '{working_dir}') first, then retry.\n\nError: {output[-1000:]}"
            )
        if any(k in low for k in ("connection refused", "connection closed", "error during connect", "cannot connect to the docker daemon")):
            return (
                f"FAILED — Docker daemon connection error (Docker Desktop may be blocked by a "
                f"file bind-mount or the daemon is not running).\n"
                f"Check containers anyway with compose_ps('{project_name}') — Docker may have "
                f"started containers before the error.\nError: {output[-1000:]}"
            )
        return f"FAILED (exit code {proc.returncode}):\n{output}"
    except asyncio.TimeoutError:
        # Process still running after 20s — that's normal for slow image pulls or health-checks.
        # Return now so the MCP connection stays alive. Agent should poll compose_ps().
        log_f.close()
        partial = ""
        try:
            partial = open(log_path).read()[-500:].strip()
        except Exception:
            pass
        return (
            f"STARTED — docker compose up -d is running in the background.\n"
            f"Call compose_ps('{project_name}') every 5s to watch containers come up.\n"
            f"Recent output:\n{partial}"
        )


@mcp.tool()
async def compose_down(project_name: str) -> str:
    """Stop a Docker Compose stack (without removing volumes)."""
    # NOTE: -v flag is blocked by docker_guard (prevents data loss).
    # Use compose down without -v for safety.
    rc, output = await run_docker(
        "docker", "compose", "-p", project_name, "down", "--remove-orphans",
        actor="mcp_docker",
    )
    return output


@mcp.tool()
async def compose_ps(project_name: str) -> str:
    """List containers in a Docker Compose stack with their status."""
    rc, output = await run_docker(
        "docker", "compose", "-p", project_name, "ps",
        actor="mcp_docker",
    )
    return output


# ── Tunnel Management ──────────────────────────────────────────────

@mcp.tool()
async def tunnel_start(service_name: str, target_url: str, host_header: str = "") -> str:
    """Start a Cloudflare tunnel for a service. Idempotent — returns existing URL if already running.

    Args:
        service_name: Unique name (e.g. project name "tagh-test")
        target_url: URL to proxy to (e.g. "http://172.21.0.7:80")
        host_header: Optional Host header override for virtual host matching
    """
    from openclow.services.tunnel_service import start_tunnel
    url = await start_tunnel(service_name, target_url, host_header=host_header or None)
    if url:
        return f"SUCCESS: Tunnel running at {url}"
    return "FAILED: Could not start tunnel — check cloudflared is installed"


@mcp.tool()
async def tunnel_stop(service_name: str) -> str:
    """Stop a running tunnel by service name."""
    from openclow.services.tunnel_service import stop_tunnel
    await stop_tunnel(service_name)
    return f"Tunnel '{service_name}' stopped"


@mcp.tool()
async def tunnel_get_url(service_name: str) -> str:
    """Get the current public URL for a tunnel. Returns empty string if not running."""
    from openclow.services.tunnel_service import get_tunnel_url
    url = await get_tunnel_url(service_name)
    return url or "No tunnel running for this service"


@mcp.tool()
async def tunnel_list() -> str:
    """List all active tunnels with their URLs."""
    from openclow.services.config_service import get_all_config
    configs = await get_all_config()
    tunnels = []
    for key, val in configs.items():
        if key.startswith("tunnel.") and val.get("url"):
            name = key.split(".", 1)[1]
            tunnels.append(f"  {name}: {val['url']} → {val.get('target', '?')}")
    return "\n".join(tunnels) or "No active tunnels"


if __name__ == "__main__":
    mcp.run(transport="stdio")
