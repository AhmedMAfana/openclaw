"""Docker MCP Server — gives agents direct access to Docker operations.

All commands go through docker_guard for allowlist enforcement and audit logging.
Agents cannot run destructive Docker commands (rm, prune, rmi, etc.).
"""
import json
import shlex

from mcp.server.fastmcp import FastMCP

from openclow.services.docker_guard import run_docker

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
    """Execute a command inside a running Docker container. Returns exit code and output."""
    cmd_parts = shlex.split(command)
    rc, output = await run_docker(
        "docker", "exec", container_name, *cmd_parts,
        actor="mcp_docker",
    )
    if rc != 0:
        return f"FAILED (exit code {rc}):\n{output[-5000:]}"
    return output[-5000:]


@mcp.tool()
async def compose_build(compose_file: str, project_name: str, working_dir: str) -> str:
    """Build images for a Docker Compose stack (without starting containers).

    Use this before compose_up() when the project has custom Dockerfiles.
    This runs without --project-directory so build contexts resolve correctly
    inside the worker container (Docker-in-Docker).
    """
    args = ["docker", "compose", "-f", compose_file, "-p", project_name, "build"]
    rc, output = await run_docker(*args, actor="mcp_docker", cwd=working_dir, timeout=600)
    if rc != 0:
        return f"FAILED (exit code {rc}):\n{output}"
    return f"SUCCESS:\n{output}"


@mcp.tool()
async def compose_up(compose_file: str, project_name: str, working_dir: str, build: bool = False) -> str:
    """Start a Docker Compose stack. Returns output with SUCCESS/FAILED prefix.

    Args:
        build: If True, build images first then start. Handles Docker-in-Docker
               path translation automatically (builds use container paths for
               build contexts, up uses host paths for volume mounts).
    """
    # When build=True, run build and up as separate steps.
    # Build needs container filesystem paths (Docker CLI tars build context).
    # Up needs host filesystem paths (Docker daemon resolves volume mounts).
    # docker_guard handles this: skips --project-directory for build commands.
    if build:
        build_args = ["docker", "compose", "-f", compose_file, "-p", project_name, "build"]
        rc, output = await run_docker(*build_args, actor="mcp_docker", cwd=working_dir, timeout=600)
        if rc != 0:
            return f"FAILED (build, exit code {rc}):\n{output}"

    up_args = ["docker", "compose", "-f", compose_file, "-p", project_name, "up", "-d"]
    rc, output = await run_docker(*up_args, actor="mcp_docker", cwd=working_dir, timeout=300)

    if rc != 0 and not build:
        # Image might not exist yet — try building first then starting
        if "pull access denied" in output or "not found" in output.lower() or "no such image" in output.lower():
            build_args = ["docker", "compose", "-f", compose_file, "-p", project_name, "build"]
            rc2, out2 = await run_docker(*build_args, actor="mcp_docker", cwd=working_dir, timeout=600)
            if rc2 != 0:
                return f"FAILED (auto-build, exit code {rc2}):\n{out2}"
            rc3, out3 = await run_docker(*up_args, actor="mcp_docker", cwd=working_dir, timeout=300)
            if rc3 == 0:
                return f"SUCCESS (auto-built):\n{out3}"
            return f"FAILED (exit code {rc3}):\n{out3}"

    if rc != 0:
        return f"FAILED (exit code {rc}):\n{output}"
    return f"SUCCESS:\n{output}"


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
