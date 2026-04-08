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
    """Execute a command inside a running Docker container."""
    cmd_parts = shlex.split(command)
    rc, output = await run_docker(
        "docker", "exec", container_name, *cmd_parts,
        actor="mcp_docker",
    )
    return output[-5000:]


@mcp.tool()
async def compose_up(compose_file: str, project_name: str, working_dir: str) -> str:
    """Start a Docker Compose stack."""
    rc, output = await run_docker(
        "docker", "compose", "-f", compose_file, "-p", project_name, "up", "-d",
        actor="mcp_docker", cwd=working_dir,
    )
    return output


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


if __name__ == "__main__":
    mcp.run(transport="stdio")
