"""Bootstrap Agent — auto-setup a project's full Docker environment.

Uses Claude CLI subprocess (not SDK) for reliability.
"""
import asyncio
import json

from openclow.utils.logging import get_logger

log = get_logger()

BOOTSTRAP_PROMPT = """You are a DevOps engineer. Get this project running.

Project: {project_name} | Tech: {tech_stack}
Docker Compose: {docker_compose_file}
App Container: {app_container} | Port: {app_port}
Working Directory: {workspace_path}

DO THIS IN ORDER:
1. Check if .env exists. If not, copy .env.example to .env
2. Run: docker compose -f {docker_compose_file} -p openclow-{project_name} up -d
3. Wait 15 seconds, then check: docker compose -p openclow-{project_name} ps
4. For any unhealthy container, read its logs and fix the issue
5. Check if app responds: curl -s http://localhost:{app_port} || echo "not responding"
6. Report what happened"""


async def run_bootstrap(workspace_path: str, project) -> str:
    """Run bootstrap via Claude CLI subprocess."""
    prompt = BOOTSTRAP_PROMPT.format(
        project_name=project.name,
        tech_stack=project.tech_stack or "Unknown",
        docker_compose_file=project.docker_compose_file or "docker-compose.yml",
        app_container=project.app_container_name or "app",
        app_port=project.app_port or 8000,
        workspace_path=workspace_path,
    )

    log.info("bootstrap.started", project=project.name, workspace=workspace_path)

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--output-format", "json",
            "--max-turns", "20",
            "--allowedTools", "Bash,Read,Write,Edit,Glob",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_path,
        )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode == 0:
            data = json.loads(stdout.decode())
            result = data.get("result", "")
            log.info("bootstrap.done", project=project.name, turns=data.get("num_turns", 0))
            return result
        else:
            error = stderr.decode()[:500]
            log.error("bootstrap.cli_failed", returncode=proc.returncode, error=error)
            return f"Bootstrap failed: {error}"

    except asyncio.TimeoutError:
        log.error("bootstrap.timeout", project=project.name)
        if proc:
            proc.kill()
        return "Bootstrap timed out after 5 minutes"
    except Exception as e:
        log.error("bootstrap.error", error=str(e))
        return f"Bootstrap error: {str(e)}"
