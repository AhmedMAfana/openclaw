"""Bootstrap Agent — auto-setup a project's full Docker environment.

Uses Claude CLI subprocess (not SDK) for reliability.
"""
import asyncio
import json

from openclow.utils.logging import get_logger

log = get_logger()

BOOTSTRAP_PROMPT = """You are a DevOps engineer. Get this project running from scratch.

Project: {project_name}
Tech Stack: {tech_stack}
Docker Compose file: {docker_compose_file}
App service name: {app_container}
App port: {app_port}
Working directory: {workspace_path}

## Steps — Complete Each Before Moving to the Next

### Step 1: Prepare environment
- Check if .env exists. If not: copy .env.example to .env
- Verify the .env has a valid APP_KEY or SECRET_KEY (generate one if missing)
- If there is a composer.lock but no auth.json, check /app/auth.json and copy it here

### Step 2: Start containers
Run: docker compose -f {docker_compose_file} -p openclow-{project_name} up -d --build
Wait 20 seconds after the command completes.

### Step 3: Check container health
Run: docker compose -p openclow-{project_name} ps
For each container NOT in "healthy" or "running" state:
  a. Read its logs: docker compose -p openclow-{project_name} logs [service]
  b. Diagnose the specific error (missing package, wrong env var, port conflict, etc.)
  c. Apply the targeted fix (edit Dockerfile, .env, requirements.txt, etc.)
  d. Rebuild that service: docker compose -f {docker_compose_file} -p openclow-{project_name} up -d --build [service]
  e. Wait 15 seconds, re-check
Repeat until all containers are running.

### Step 4: Run post-start setup (if needed by tech stack)
Common setup commands:
- PHP/Laravel: docker compose exec {app_container} php artisan migrate --force
- Python/Django: docker compose exec {app_container} python manage.py migrate
- Node.js: skip (no migrations needed usually)
- Ruby/Rails: docker compose exec {app_container} rails db:migrate
Only run migrations if you see database models/migration files in the workspace.

### Step 5: Verify the app responds
Run inside the app container:
  curl -s -o /dev/null -w "%{{http_code}}" http://localhost:{app_port}
- 200–399: success
- 500: app crashed — read its logs and fix
- Connection refused: container may need restart or app hasn't bound to port yet

### Step 6: Final report
End with exactly one of:
BOOTSTRAP_SUCCESS: App is running at localhost:{app_port} — [what was fixed if anything]
BOOTSTRAP_FAILED: [specific reason] — [exact command the user should run manually]
"""


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
