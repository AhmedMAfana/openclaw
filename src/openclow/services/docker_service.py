"""Docker service — monitors and manages project containers.

All Docker commands go through docker_guard for allowlist + audit logging.
"""
import asyncio
import json
from dataclasses import dataclass

from openclow.utils.logging import get_logger
from openclow.services.docker_guard import run_docker, run_docker_compose

log = get_logger()


@dataclass
class ContainerStatus:
    name: str
    status: str  # running, exited, restarting, unhealthy
    health: str  # healthy, unhealthy, starting, none
    port: str | None


async def get_project_containers(project_name: str, task_id: str) -> list[ContainerStatus]:
    """Get status of all containers for a project."""
    docker_project = f"openclow-{project_name}-{task_id[:8]}"
    try:
        rc, output = await run_docker_compose(
            "ps", "--format", "json",
            compose_project=docker_project,
            compose_file="",
            actor="docker_service",
            project_name=project_name,
        )
        if rc != 0 or not output:
            return []

        containers = []
        for line in output.strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                containers.append(ContainerStatus(
                    name=data.get("Name", ""),
                    status=data.get("State", "unknown"),
                    health=data.get("Health", "none"),
                    port=data.get("Publishers", [{}])[0].get("PublishedPort") if data.get("Publishers") else None,
                ))
            except (json.JSONDecodeError, IndexError, KeyError):
                continue
        return containers
    except Exception as e:
        log.error("docker.status_failed", error=str(e))
        return []


async def get_container_logs(container_name: str, tail: int = 50) -> str:
    """Get recent logs from a container."""
    try:
        rc, output = await run_docker(
            "docker", "logs", container_name, "--tail", str(tail),
            actor="docker_service",
        )
        return output
    except Exception:
        return ""


async def is_project_healthy(project_name: str, task_id: str) -> tuple[bool, list[ContainerStatus]]:
    """Check if all project containers are healthy."""
    containers = await get_project_containers(project_name, task_id)
    if not containers:
        return False, []

    all_healthy = all(
        c.status == "running" and c.health in ("healthy", "none")
        for c in containers
    )
    return all_healthy, containers


async def start_project(workspace_path: str, compose_file: str, project_name: str, task_id: str) -> list[ContainerStatus]:
    """Start a project's Docker stack and return container statuses."""
    docker_project = f"openclow-{project_name}-{task_id[:8]}"
    await run_docker_compose(
        "up", "-d", "--build",
        compose_file=compose_file,
        compose_project=docker_project,
        actor="docker_service",
        project_name=project_name,
        cwd=workspace_path,
    )
    await asyncio.sleep(10)
    return await get_project_containers(project_name, task_id)


async def stop_project(workspace_path: str, project_name: str, task_id: str):
    """Stop a project's Docker stack."""
    docker_project = f"openclow-{project_name}-{task_id[:8]}"
    # NOTE: no -v flag — docker_guard blocks volume removal for safety.
    await run_docker_compose(
        "down", "--remove-orphans",
        compose_project=docker_project,
        compose_file="",
        actor="docker_service",
        project_name=project_name,
        cwd=workspace_path,
    )


def format_status(containers: list[ContainerStatus]) -> str:
    """Format container statuses for display."""
    if not containers:
        return "No containers running"

    lines = []
    for c in containers:
        icon = "✅" if c.status == "running" and c.health in ("healthy", "none") else "❌"
        health_str = f" ({c.health})" if c.health != "none" else ""
        port_str = f" → port {c.port}" if c.port else ""
        lines.append(f"  {icon} {c.name}: {c.status}{health_str}{port_str}")
    return "\n".join(lines)
