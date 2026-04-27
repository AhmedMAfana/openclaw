"""Unified command execution against a project, regardless of whether it's a
Docker-managed stack (mode="docker") or an already-running host app (mode="host").

Used by the coder/deploy/health tasks so they don't need to branch on project.mode
everywhere. Agent tool selection (mcp__docker__* vs mcp__host__*) still happens
at the LLM layer; this helper is for the deterministic Python paths (frontend
build step, lightweight deploy, health probe curl).
"""
from __future__ import annotations

from openclow.models.project import Project


async def execute_in_project(
    project: Project,
    command: str,
    *,
    timeout: int = 60,
    actor: str = "task",
) -> tuple[int, str]:
    """Run a shell command against the project. Returns (returncode, output)."""
    if (getattr(project, "mode", "docker") or "docker") == "host":
        from openclow.services.host_guard import run_host
        return await run_host(
            command,
            cwd=project.project_dir or "",
            timeout=timeout,
            actor=actor,
            project_name=project.name,
            project_id=project.id,
        )

    # Docker mode — exec inside the app container.
    from openclow.services.docker_guard import run_docker
    container = f"openclow-{project.name}-{project.app_container_name or 'app'}-1"
    return await run_docker(
        "docker", "exec", container, "sh", "-c", command,
        actor=actor,
        project_name=project.name,
        project_id=project.id,
        timeout=timeout,
    )
