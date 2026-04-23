"""Bootstrap task — agentic project setup with self-healing.

NOT a one-shot script. This is a full end-to-end agentic mission:
1. Clone repo → check .env → docker compose up
2. If build fails → Doctor diagnoses + fixes → retry build
3. If containers unhealthy → Doctor reads logs → fixes → restarts
4. If app not responding → Doctor investigates → fixes
5. Start Cloudflare tunnel → generate public URL
6. Playwright visits the app → verifies it actually works → screenshots
7. If Playwright sees errors → Doctor reads logs → fixes → retry
8. Every single step reported to Telegram in real-time
9. If can't fix → clear explanation of what's wrong + what user needs to do
10. Keeps going until everything is healthy OR all options exhausted
"""
import asyncio
import os
import re
import shutil

from openclow.utils.docker_path import get_docker_env
import time

from sqlalchemy import select

from openclow.models import Project, async_session
from openclow.providers import factory
from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()

# Appended to every sub-agent system prompt — prevents the single most common hang.
_NO_DOCKER_SOCKET = (
    " NEVER run 'curl --unix-socket /run/docker.sock' or any raw Docker API call"
    " via docker_exec inside a project container — the socket is NOT mounted there"
    " and the call hangs forever. Use ONLY MCP tools (compose_ps, compose_up,"
    " container_logs, docker_exec for app commands) for all Docker operations."
)


# ---------------------------------------------------------------------------
# Master Agent Bootstrap Prompt
# ---------------------------------------------------------------------------

MASTER_BOOTSTRAP_PROMPT = """You are setting up a project from scratch. You have FULL CONTROL.

PROJECT: {project_name}
TECH STACK: {tech_stack}
WORKSPACE: {workspace}
COMPOSE FILE: {compose}
COMPOSE PROJECT: {compose_project}
HOST ARCHITECTURE: {arch}
ALLOCATED PORT: {port}

DOCKER-COMPOSE CONTENTS:
```yaml
{compose_contents}
```

.ENV CONTENTS:
```
{env_contents}
```

YOUR MISSION — execute these steps IN ORDER:

STEP 2 — INSTALL DEPENDENCIES:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE: If docker-compose.yml OR docker-compose.yaml EXISTS in the workspace →
      output STEP_SKIP: 2 docker-handles-deps IMMEDIATELY. No reading package.json.
      No running npm/composer/pip. Docker build handles all dependencies.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For non-dockerized projects ONLY: run the install command on the host.
Output: STEP_DONE: 2 <short result> OR STEP_SKIP: 2 <reason>

STEP 3 — BUILD FRONTEND:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE: If docker-compose.yml EXISTS → output STEP_SKIP: 3 docker-handles-build IMMEDIATELY.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For non-dockerized projects ONLY: check if assets need building and build them.
Output: STEP_DONE: 3 <short result> OR STEP_SKIP: 3 <reason>

STEP 4 — START DOCKER CONTAINERS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANDATORY: Always call compose_build() FIRST, then compose_up().
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

4a. Build images:
    STATUS: Building Docker images — this may take 5-20 minutes, please wait...
    → compose_build("{compose}", "{compose_project}", "{workspace}")

    compose_build responses:
    - "DONE ..." → build succeeded, proceed to 4b immediately
    - "FAILED ..." → read the error, fix the Dockerfile/.env/docker-compose.yml, retry once
    - "BUILDING ..." → build is running in the background (ARM builds take 5-20min).
      YOU MUST poll with compose_build_status("{compose_project}") every 30s.
      Keep polling until it returns "DONE" or "FAILED". Output STATUS: Building... each poll.
      Do NOT call compose_up until compose_build_status returns "DONE".
      Do NOT output STEP_DONE: 4 until containers are actually confirmed running.

4b. Start containers (fast, <30s):
    STATUS: Starting containers...
    → compose_up("{compose}", "{compose_project}", "{workspace}")

4c. Verify all containers are running:
    → compose_ps("{compose_project}")
    → list_containers("{compose_project}")
    For any container NOT running: read container_logs, diagnose, fix.
    You get up to 3 fix attempts total.

- Output: STEP_DONE: 4 <X/Y containers running>

STEP 5 — DATABASE MIGRATIONS:
- Identify the framework from project files (Laravel→artisan, Django→manage.py, Rails→rake, Node→prisma/knex/etc.)
- Find the app container (not mysql/redis/postgres — the actual app)
- BEFORE running migrations, check the container's working directory:
  * Use docker_exec to run `pwd` and `ls` to find where the app code lives
  * The artisan/manage.py file may NOT be in the default workdir (e.g. workdir=/var/www but code is in /var/www/html)
  * Always use the FULL PATH to the migration command (e.g. `php /var/www/html/artisan migrate --force`)
- Use Docker MCP docker_exec to run migrations inside the container
- Run seeders if they exist (e.g. `php /var/www/html/artisan db:seed --force`)
- If a command fails:
  * READ the error message carefully
  * DIAGNOSE: check workdir, find where the file actually is, check permissions
  * FIX: use the correct path, or cd to the right directory
  * Output DIAGNOSIS: <what went wrong> and ACTION: <what you're fixing>
  * Retry with the fix
- If DB not ready, wait and retry (up to 3 times)
- Output: STEP_DONE: 5 <what you did> OR STEP_SKIP: 5 <reason>

STEP 6 — VERIFY APP:
- Use Docker MCP docker_exec to curl localhost:<internal_port> inside the app container
- Check for HTTP 200/301/302
- If it fails, read container logs, diagnose, and try to fix
- Report the HTTP status code
- Output: STEP_DONE: 6 <HTTP status> OR STEP_FAIL: 6 <error>

RULES:
- Output STATUS: <message> BEFORE every action (so user sees live progress)
- Output DIAGNOSIS: <analysis> when something fails (so user understands WHY)
- Output ACTION: <what you're doing> when fixing something
- Output STEP_DONE: <N> <summary> when a step succeeds
- Output STEP_SKIP: <N> <reason> when a step should be skipped
- Output STEP_FAIL: <N> <error> when a step fails after retries
- Output BOOTSTRAP_COMPLETE when all steps done
- Output BOOTSTRAP_FAILED: <reason> if you cannot continue
- Be FAST — don't over-analyze, act decisively
- Be SURGICAL — only change what's broken
- You CAN modify docker-compose.yml, .env, Dockerfiles — whatever it takes

CRITICAL — TOOL USAGE:
- You do NOT have Bash access. Use ONLY Docker MCP tools for all container operations.
- To run any command in a container: use docker_exec(container_name, "command here")
- To check files on the HOST workspace: use Read, Glob, Grep
- To modify files on the HOST workspace: use Write, Edit
- To start/stop Docker: use compose_up, compose_down, compose_ps

CRITICAL — SELF-HEALING ON FAILURE:
- When ANY command fails (look for "FAILED" in the output), you MUST investigate before giving up:
  1. Read the error message — what exactly failed?
  2. Check the environment — use docker_exec to run `pwd`, `ls`, `which`, `find`
  3. Fix the root cause (wrong path? missing file? wrong workdir?)
  4. Retry with the fix
  5. Only STEP_FAIL after you've tried at least 2 different approaches
- NEVER mark a step as failed just because the first command didn't work
- You are a senior DevOps engineer — debug it like one
"""


# ---------------------------------------------------------------------------
# Project status helper
# ---------------------------------------------------------------------------

async def _set_project_status(project_id: int, status: str):
    """Update project status in DB. Valid: bootstrapping, active, failed, inactive."""
    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        proj = result.scalar_one_or_none()
        if proj:
            proj.status = status
            await session.commit()


async def _save_project_port(project_id: int, port: int):
    """Persist the allocated host port to project.app_port.

    Required so the tunnel health loop (which filters by app_port IS NOT NULL)
    can find this project and auto-restart its tunnel if it dies.
    """
    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        proj = result.scalar_one_or_none()
        if proj and proj.app_port != port:
            proj.app_port = port
            await session.commit()


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

async def _run(*args: str, cwd: str = None, timeout: int = 300,
               project_id: int | None = None) -> tuple[int, str]:
    """Run a command safely, return (returncode, output).

    Routes Docker commands through docker_guard for allowlist enforcement.
    All commands are audit-logged.
    Docker compose builds get extended timeout (600s).
    """
    from openclow.services.audit_service import log_action

    # Route Docker commands through the guard
    if args and args[0] == "docker":
        from openclow.services.docker_guard import run_docker
        # Docker compose build/up needs more time than 300s
        docker_timeout = timeout
        cmd_str = " ".join(args)
        if "compose" in cmd_str and ("up" in cmd_str or "build" in cmd_str):
            docker_timeout = max(timeout, 600)
        return await run_docker(
            *args, actor="bootstrap", project_id=project_id,
            cwd=cwd, timeout=docker_timeout,
        )

    env = get_docker_env({**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    try:
        from openclow.services.config_service import get_config
        config = await get_config("git", "provider")
        if config and config.get("token"):
            env["GH_TOKEN"] = config["token"]
            env["GITHUB_TOKEN"] = config["token"]
    except Exception:
        pass

    cmd_str = " ".join(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        combined = (stdout.decode() + stderr.decode()).strip()
        rc = proc.returncode
        output = combined[-4000:]
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        rc, output = -1, f"TIMEOUT after {timeout}s"
    except Exception as e:
        rc, output = -1, str(e)

    await log_action(
        actor="bootstrap", action="bash", command=cmd_str,
        workspace=cwd, exit_code=rc, output_summary=output[:2000],
    )
    return rc, output


# Telegram reporters (shared services)
# ---------------------------------------------------------------------------

from openclow.services.checklist_reporter import ChecklistReporter  # noqa: E402


# ---------------------------------------------------------------------------
# Bootstrap steps
# ---------------------------------------------------------------------------

async def _step_clone(on_progress, project, workspace: str) -> str:
    """Step 1: Clone or update the repository. Returns detail string."""
    await on_progress("Cloning repository...")

    if not os.path.exists(workspace):
        git_provider = await factory.get_git()
        await git_provider.clone_repo(project.github_repo, workspace)
        return "cloned"
    else:
        # Fetch + reset to handle divergent branches cleanly
        await _run("git", "-C", workspace, "fetch", "origin",
                   project.default_branch, cwd=workspace)
        rc, output = await _run("git", "-C", workspace, "reset", "--hard",
                                f"origin/{project.default_branch}", cwd=workspace)
        if rc == 0:
            return "updated"
        else:
            return f"warning: {output[:50]}"


async def _step_env(on_progress, workspace: str, compose: str) -> str:
    """Step 2: Check/setup .env file. Returns detail string."""
    await on_progress("Checking environment...")

    compose_dir = os.path.dirname(os.path.join(workspace, compose))
    env_path = os.path.join(compose_dir, ".env")
    env_example = os.path.join(compose_dir, ".env.example")

    if not os.path.exists(env_path) and os.path.exists(env_example):
        shutil.copy2(env_example, env_path)
        return "copied .env.example → .env"
    elif os.path.exists(env_path):
        return ".env exists"
    else:
        return "no .env found (may be fine)"


def _ensure_restart_policy(workspace: str) -> None:
    """Inject `restart: unless-stopped` into every service via compose override.

    Framework compose files (Laravel Sail, etc.) don't include restart policies, so
    containers die when Docker restarts and never come back. We create/update a
    docker-compose.override.yml in the workspace that adds the policy to all services.

    Idempotent — regenerates the file each time, which is safe.
    """
    import subprocess

    compose_path = os.path.join(workspace, "docker-compose.yml")
    if not os.path.exists(compose_path):
        return

    # Read service names from the compose file
    result = subprocess.run(
        ["docker", "compose", "-f", compose_path, "config", "--services"],
        capture_output=True, text=True, timeout=10, env=get_docker_env(),
    )
    if result.returncode != 0:
        return

    services = [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]
    if not services:
        return

    lines = ["# Auto-generated by TAGH Dev preflight — adds restart policy to all services",
             "# Ensures containers survive Docker/worker restarts without manual intervention",
             "services:"]
    for svc in services:
        lines.append(f"  {svc}:")
        lines.append("    restart: unless-stopped")

    override_path = os.path.join(workspace, "docker-compose.override.yml")
    with open(override_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    log.info("preflight.restart_policy_written", services=services)


def _patch_sail_dockerfile_if_needed(workspace: str) -> None:
    """Patch Laravel Sail vendor Dockerfile to survive npm compatibility issues.

    On arm64 (Apple Silicon) with certain Node.js versions, `npm install -g npm`
    fails because of broken native modules (e.g. promise-retry). Making the step
    non-fatal with `|| true` lets the build complete with the bundled npm version,
    which is perfectly fine for Laravel development.

    Only patches when the sail-8.4/app image doesn't exist yet — once the image is
    built, it's shared across ALL projects (Docker image name is fixed) so this code
    path is skipped on every subsequent bootstrap.
    Idempotent: safe to call multiple times.
    """
    import re
    import subprocess

    # Skip if the shared sail image already exists — no rebuild will happen
    check = subprocess.run(
        ["docker", "image", "inspect", "sail-8.4/app"],
        capture_output=True, timeout=5, env=get_docker_env(),
    )
    if check.returncode == 0:
        return  # Image cached — Dockerfile won't be executed

    sail_dockerfile = os.path.join(workspace, "vendor/laravel/sail/runtimes/8.4/Dockerfile")
    if not os.path.exists(sail_dockerfile):
        return  # Not a Laravel Sail 8.4 project

    with open(sail_dockerfile) as f:
        content = f.read()

    # Replace `npm install -g npm \` with the non-fatal variant (idempotent via negative lookahead)
    patched, n = re.subn(
        r'(npm install -g npm)(?!\s*--force)(\s*\\)',
        r'\1 --force 2>/dev/null || true\2',
        content,
    )
    if n:
        with open(sail_dockerfile, "w") as f:
            f.write(patched)
        log.info("preflight.sail_dockerfile_patched", path=sail_dockerfile,
                 reason="npm self-upgrade non-fatal (arm64 + Node.js compatibility)")


async def _preflight(project, workspace: str, compose: str, compose_project: str):
    """Pre-bootstrap/repair cleanup and verification. Runs BEFORE the agent starts.

    Handles infrastructure problems that an LLM agent can't fix:
    1. Verify Docker daemon is accessible
    2. Kill old containers for this project (prevent port conflicts + orphans)
    3. Kill orphan stacks with task ID suffixes
    4. Prune dangling networks
    5. Write port env vars into .env
    6. Verify Docker-in-Docker host path detection
    7. Stop old tunnel (recreated after app verified)
    8. Free up ports — stop anything using our allocated ports
    9. Verify compose file exists
    """
    from openclow.services.docker_guard import run_docker_compose, _detect_host_workspace_path
    from openclow.services.port_allocator import get_port_env_vars

    # 1. Verify Docker daemon is accessible
    _denv = get_docker_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info", "--format", "{{.ServerVersion}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_denv,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            log.error("preflight.docker_unavailable", stderr=stderr.decode()[:200])
            raise RuntimeError("Docker daemon is not running or not accessible")
    except FileNotFoundError:
        raise RuntimeError("Docker CLI not found")

    # 2. Stop any old containers for this project (both naming patterns)
    for proj_name in [compose_project, project.name]:
        try:
            await run_docker_compose(
                "down", "--remove-orphans",
                compose_project=proj_name,
                actor="preflight", project_name=project.name,
                cwd=workspace, timeout=30,
            )
        except Exception:
            pass

    # 3. Kill orphan stacks with task ID suffixes (openclow-{name}-{taskid})
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--filter", f"name=openclow-{project.name}-",
            "--format", "{{.Names}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_denv,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        orphans = set()
        for name in stdout.decode().strip().split("\n"):
            name = name.strip()
            if not name:
                continue
            parts = name.rsplit("-", 1)
            if len(parts) == 2:
                stack = parts[0].rsplit("-", 1)[0]
                if stack != compose_project and stack.startswith(f"openclow-{project.name}"):
                    orphans.add(stack)
        for orphan in orphans:
            log.warning("preflight.cleaning_orphan", stack=orphan)
            orphan_proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "-p", orphan, "down", "--remove-orphans",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=_denv,
            )
            await asyncio.wait_for(orphan_proc.communicate(), timeout=30)
    except Exception:
        pass

    # 4. Prune dangling networks (leftover from failed compose down)
    try:
        prune_proc = await asyncio.create_subprocess_exec(
            "docker", "network", "prune", "-f",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_denv,
        )
        await asyncio.wait_for(prune_proc.communicate(), timeout=10)
    except Exception:
        pass

    # 5. Write port env vars directly into .env
    port_vars = get_port_env_vars(project.id)
    env_path = os.path.join(workspace, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            env_content = f.read()
        lines = [l for l in env_content.split("\n")
                 if not any(l.startswith(f"{k}=") for k in port_vars)]
        lines.append("\n# TAGH Dev port isolation (auto-generated)")
        for k, v in port_vars.items():
            lines.append(f"{k}={v}")
        with open(env_path, "w") as f:
            f.write("\n".join(lines))

    # 6. Verify Docker-in-Docker path detection
    host_path = await _detect_host_workspace_path()
    if not host_path:
        log.warning("preflight.no_host_path",
                    hint="Docker-in-Docker path detection failed — volume mounts may not work")

    # 7. Stop old tunnel (recreated after app verified)
    try:
        from openclow.services.tunnel_service import stop_tunnel
        await stop_tunnel(project.name)
    except Exception:
        pass

    # 8. Free up ports — stop anything using our allocated ports
    app_port = port_vars.get("APP_PORT", "")
    if app_port:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "ps", "--filter", f"publish={app_port}", "--format", "{{.Names}}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=_denv,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            for container in stdout.decode().strip().split("\n"):
                container = container.strip()
                if container and container != f"{compose_project}-{project.app_container_name or 'app'}-1":
                    stop_proc = await asyncio.create_subprocess_exec(
                        "docker", "stop", container,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        env=_denv,
                    )
                    await asyncio.wait_for(stop_proc.communicate(), timeout=10)
                    log.info("preflight.stopped_port_conflict", container=container, port=app_port)
        except Exception:
            pass

    # 9. Verify compose file exists
    compose_path = os.path.join(workspace, compose)
    if not os.path.exists(compose_path):
        raise RuntimeError(f"Compose file not found: {compose_path}")

    # 10. Patch sail vendor Dockerfile for arm64/Node.js npm compatibility
    #     `npm install -g npm` fails on certain Node/arm64 combos (missing promise-retry).
    #     Makes it non-fatal so builds succeed. Only runs when sail-8.4/app image is missing.
    _patch_sail_dockerfile_if_needed(workspace)

    # 11. Ensure restart policy — project containers survive worker/Docker restarts
    #     Docker Compose files from frameworks (Laravel Sail, etc.) don't include restart
    #     policies. We inject them via compose override so containers auto-recover.
    _ensure_restart_policy(workspace)

    log.info("preflight.done", project=project.name, host_path=host_path,
             ports=f"app={app_port}")


def _parse_docker_error(output: str) -> str:
    """Extract the most relevant error from docker compose output."""
    if not output:
        return "compose failed (no output)"
    # Return last meaningful line — Docker puts the error at the end
    lines = [l.strip() for l in output.strip().split("\n") if l.strip()]
    # Filter out pure progress lines
    meaningful = [l for l in lines if not l.startswith("=>") and not l.startswith("#")]
    if meaningful:
        return meaningful[-1][:200]
    return lines[-1][:200] if lines else "compose failed"


async def _get_compose_containers(
    compose_project: str, workspace: str, project_id: int | None = None,
) -> list[dict]:
    """Get container status via docker ps (not compose ps — avoids config mismatch)."""
    import json as _json
    # Use docker ps with project filter — sees ALL containers regardless of env vars
    rc, ps_output = await _run(
        "docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={compose_project}",
        "--format", '{"Name":{{json .Names}},"State":{{json .State}},"Status":{{json .Status}}}',
    )
    if rc != 0 or not ps_output.strip():
        return []

    containers = []
    for line in ps_output.strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = _json.loads(line)
            full_name = data.get("Name", "")
            # Extract service name: "openclow-tagh-test-nginx-1" → "nginx"
            parts = full_name.replace(compose_project + "-", "").rsplit("-", 1)
            service = parts[0] if parts else full_name
            state = data.get("State", "unknown").lower()
            containers.append({
                "name": service,
                "full_name": full_name,
                "state": state,
                "health": data.get("Status", ""),
            })
        except _json.JSONDecodeError:
            continue
    return containers


async def _get_container_error(container_name: str) -> str:
    """Get a clean one-line error from a failed container's logs."""
    if not container_name:
        return "unknown"
    rc, logs = await _run("docker", "logs", container_name, "--tail", "10")
    if rc != 0 or not logs.strip():
        return "no logs"
    # Find the most relevant error line
    for line in reversed(logs.strip().split("\n")):
        line = line.strip()
        if any(kw in line.lower() for kw in ("error", "fatal", "denied", "failed", "panic", "exception")):
            return line[:80]
    # No explicit error — return last line
    return logs.strip().split("\n")[-1][:80]


async def _get_container_workdir(container_name: str) -> str | None:
    """Detect a container's working directory via docker inspect.

    Returns the WORKDIR set in the Dockerfile (e.g. /var/www/html, /app, /usr/src/app).
    Falls back to None if detection fails.
    """
    rc, output = await _run(
        "docker", "inspect", container_name,
        "--format", "{{.Config.WorkingDir}}",
    )
    if rc == 0 and output.strip() and output.strip() != "/":
        return output.strip()
    return None


async def _configure_app_for_tunnel(workspace: str, tunnel_url: str, compose_project: str):
    """Configure the app's .env to work with the Cloudflare tunnel URL.

    Generic — handles any framework:
    - Laravel: APP_URL, ASSET_URL, VITE_API_BASE_URL, FORCE_HTTPS, TRUSTED_PROXIES
    - Node/Next: NEXT_PUBLIC_URL, BASE_URL
    - Django: ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS
    - Any: rewrites URL-related env vars from http://local → https://tunnel

    Also updates nginx server_name to accept the tunnel domain.
    """
    env_path = os.path.join(workspace, ".env")
    if not os.path.exists(env_path):
        return

    # Read current .env
    with open(env_path) as f:
        lines = f.readlines()

    # URL env vars to update (key → new value)
    # ASSET_URL must be the full HTTPS tunnel URL — empty causes Laravel to use
    # the request scheme (http from cloudflared) which causes mixed content.
    # APP_URL → tunnel URL (for SSO callbacks, email links, CSRF origin checks)
    # ASSET_URL → "/" (relative). Vite bakes the base URL into chunk imports at
    # compile time. With ASSET_URL=/, assets resolve to the current page origin —
    # any tunnel URL serves them correctly without a rebuild.
    url_vars = {
        "APP_URL": tunnel_url,
        "ASSET_URL": "/",
        "VITE_API_BASE_URL": "/",
        "VITE_APP_URL": "",
        "NEXT_PUBLIC_URL": tunnel_url,
        "BASE_URL": tunnel_url,
    }

    # Extra vars to add/ensure
    extra_vars = {
        "FORCE_HTTPS": "true",
        "TRUSTED_PROXIES": "*",
        "SESSION_SECURE_COOKIE": "true",
    }

    # Rewrite existing vars + track what we set
    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0]
            if key in url_vars:
                new_lines.append(f"{key}={url_vars[key]}\n")
                updated_keys.add(key)
                continue
            if key in extra_vars:
                new_lines.append(f"{key}={extra_vars[key]}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # Append vars that weren't in the file
    for key, val in {**url_vars, **extra_vars}.items():
        if key not in updated_keys:
            # Only add if it's relevant (don't add VITE_ vars to a Django project)
            if key.startswith("VITE_") and not os.path.exists(os.path.join(workspace, "vite.config")):
                if not any(os.path.exists(os.path.join(workspace, f)) for f in ["vite.config.js", "vite.config.ts"]):
                    continue
            if key.startswith("NEXT_") and not os.path.exists(os.path.join(workspace, "next.config")):
                continue
            new_lines.append(f"{key}={val}\n")

    with open(env_path, "w") as f:
        f.writelines(new_lines)

    # No nginx server_name rewrite needed — we use --http-host-header in cloudflared
    # Container .env + trustedproxy.php + cache clear handled by start_tunnel() → sync_project_tunnel()

    log.info("bootstrap.tunnel_configured", url=tunnel_url, workspace=workspace)


async def _get_tunnel_target(compose_project: str, workspace: str, project_id: int | None = None) -> str | None:
    """Get the correct tunnel target URL for a project.

    Finds the app container's IP and internal port so the tunnel
    can reach it directly — not via host port (worker can't reach host ports).
    """
    import json as _json
    app_info = await _find_app_container(compose_project, workspace, project_id)
    if not app_info:
        return None

    container_name, internal_port = app_info

    # Get the container's IP address
    rc, ip_out = await _run(
        "docker", "inspect", container_name,
        "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
    )
    if rc != 0 or not ip_out.strip():
        return None

    ip = ip_out.strip()
    return f"http://{ip}:{internal_port}"


async def _find_app_container(
    compose_project: str, workspace: str, project_id: int | None = None,
) -> tuple[str, int] | None:
    """Find the web/app container and its internal port. Generic for ANY project.

    Scans running containers for one exposing a web port (80, 443, 8080, 3000, 8000).
    Returns (container_name, internal_port) or None.
    """
    import json as _json
    web_ports = [80, 443, 8080, 3000, 8000, 5000, 4000]

    # Use docker inspect to find containers with web port mappings
    rc, output = await _run(
        "docker", "ps", "--filter", f"label=com.docker.compose.project={compose_project}",
        "--format", "{{.Names}}",
    )
    if rc != 0 or not output.strip():
        return None

    for container_name in output.strip().split("\n"):
        container_name = container_name.strip()
        if not container_name:
            continue
        # Inspect this container's port bindings
        rc2, inspect_out = await _run(
            "docker", "inspect", container_name,
            "--format", '{{json .NetworkSettings.Ports}}',
        )
        if rc2 != 0:
            continue
        try:
            ports = _json.loads(inspect_out)
            # Check in priority order (80 first, then others)
            for target in web_ports:
                port_key = f"{target}/tcp"
                if port_key in ports and ports[port_key]:
                    return (container_name, target)
        except (ValueError, _json.JSONDecodeError, TypeError):
            continue

    return None


async def _run_master_agent(
    checklist: ChecklistReporter, project, workspace: str,
    compose: str, compose_project: str, port: int,
    prompt_override: str | None = None,
    start_step: int = 2,
    max_step: int = 6,
    complete_keyword: str = "BOOTSTRAP_COMPLETE",
    failed_keyword: str = "BOOTSTRAP_FAILED",
    timeout: int = 1800,  # kept for signature compat — ignored, idle_timeout used instead
    idle_timeout: int = 1800,  # 30 min — covers docker build (up to 30 min) + compose_build blocks
) -> bool:
    """Agentic master agent with ChecklistReporter streaming.

    Uses an idle-based timeout instead of a flat wall-clock timeout.
    The agent can run as long as it keeps making tool calls or producing text.
    It is only killed if it goes `idle_timeout` seconds with zero activity
    (i.e. it is truly stuck — hung MCP call, infinite wait, etc.).

    Reusable for bootstrap (steps 2-6) and repair (steps 0-4).
    Pass prompt_override + start_step + max_step to customize.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
    except ImportError:
        # This is a fatal system error — the SDK must be installed. Never silently
        # skip the core agentic steps of a bootstrap and pretend success.
        log.error("bootstrap.master_agent_no_sdk")
        await checklist.fail_step(2, "Claude SDK not installed — admin must reinstall worker image")
        for _i in (3, 4, 5, 6):
            await checklist.fail_step(_i, "blocked: SDK missing")
        checklist._footer = "❌ Claude Agent SDK is missing from the worker container. Rebuild with `docker compose build --no-cache worker`."
        return False
    
    import platform
    from openclow.providers.llm.claude import _mcp_docker
    
    arch = platform.machine()
    
    # Read compose + env for agent context
    compose_path = os.path.join(workspace, compose)
    compose_contents = ""
    if os.path.exists(compose_path):
        with open(compose_path) as f:
            compose_contents = f.read()[:4000]
    
    env_path = os.path.join(workspace, ".env")
    env_contents = ""
    if os.path.exists(env_path):
        with open(env_path) as f:
            env_contents = f.read()[:2000]
    
    if prompt_override:
        prompt = prompt_override
    else:
        prompt = MASTER_BOOTSTRAP_PROMPT.format(
            project_name=project.name,
            tech_stack=project.tech_stack or "Unknown",
            workspace=workspace,
            compose=compose,
            compose_project=compose_project,
            arch=arch,
            port=port,
            compose_contents=compose_contents,
            env_contents=env_contents,
        )
    
    options = ClaudeAgentOptions(
        cwd=workspace,
        system_prompt=(
            f"You are a senior DevOps engineer. Your only job: get {project.name} running. Host: {arch}.\n\n"
            f"DIAGNOSTIC MINDSET — before every action:\n"
            f"1. Read the error literally. What exactly failed, and at what layer?\n"
            f"2. Check actual state with compose_ps or container_logs — don't assume.\n"
            f"3. Form one hypothesis. Act on it. Verify with a tool call.\n"
            f"4. If it didn't work: new hypothesis, different approach. Never repeat a failed action.\n\n"
            f"TOOL FAILURES ARE DATA, NOT DEAD ENDS:\n"
            f"- 'connection closed' = MCP transport hiccup. Call compose_ps IMMEDIATELY — Docker "
            f"may have succeeded before the connection dropped. If containers are up, continue.\n"
            f"- 'BLOCKED' = security guard rejected the command. Read the reason, find an alternative.\n"
            f"- 'TIMEOUT' = operation took too long. Check compose_ps — it may have finished anyway.\n"
            f"- Any other error: read it. Understand it. Fix the root cause, not the symptom.\n\n"
            f"NEVER GIVE UP: The only valid reason to output REPAIR_FAILED or BOOTSTRAP_FAILED is if "
            f"you have genuinely tried multiple different approaches and each one has a specific, "
            f"documented reason why it cannot work. 'The tool kept failing' is not a reason — "
            f"that means you need to understand WHY the tool is failing and fix that first."
            + _NO_DOCKER_SOCKET
        ),
        model="claude-sonnet-4-6",
        allowed_tools=[
            # No Bash — forces agent to use MCP Docker tools which handle errors
            # gracefully instead of crashing the SDK on non-zero exit codes.
            "Read", "Write", "Edit", "Glob", "Grep",
            # Docker MCP — the agent's ONLY interface for containers & commands
            "mcp__docker__compose_build",        # starts build; may return BUILDING after 30s
            "mcp__docker__compose_build_status", # REQUIRED: poll every 30s when compose_build returns BUILDING
            "mcp__docker__compose_up",
            "mcp__docker__compose_down",
            "mcp__docker__compose_ps",
            "mcp__docker__list_containers",
            "mcp__docker__container_logs",
            "mcp__docker__container_health",
            "mcp__docker__docker_exec",
            "mcp__docker__restart_container",
            # No tunnel tools — tunnels are step 7, handled by deterministic
            # Python code after this agent finishes steps 2-6.
        ],
        mcp_servers={
            "docker": _mcp_docker(),
        },
        permission_mode="bypassPermissions",
        max_turns=60,
    )
    
    current_step = start_step
    success = False
    docker_fix_attempts = 0
    max_docker_fixes = 3
    
    # Extract chat_id/session for web cancel checking
    _cancel_chat_id = getattr(checklist, "_chat_id", "") or ""
    _cancel_session_id = _cancel_chat_id.split(":")[2] if _cancel_chat_id.startswith("web:") and len(_cancel_chat_id.split(":")) == 3 else ""

    async def _web_cancelled() -> bool:
        """Check if web Stop was pressed for this session."""
        if not _cancel_session_id:
            return False
        try:
            import redis.asyncio as aioredis
            from openclow.settings import settings
            r = aioredis.from_url(settings.redis_url)
            val = await r.get(f"openclow:cancel_session:{_cancel_session_id}")
            await r.aclose()
            return val is not None
        except Exception:
            return False

    # Activity tracker: reset on every token/tool call — idle watchdog uses this
    _last_activity: list[float] = [time.monotonic()]

    def _bump_activity() -> None:
        _last_activity[0] = time.monotonic()

    # Heartbeat task: polls cancel flag every 5s and interrupts the stream
    _stream_task: asyncio.Task | None = None

    async def _cancel_heartbeat():
        nonlocal _stream_task
        while True:
            await asyncio.sleep(5)
            if await _web_cancelled():
                log.info("bootstrap.cancelled_by_web", project=project.name)
                if _stream_task and not _stream_task.done():
                    _stream_task.cancel()

    _hb_task = asyncio.create_task(_cancel_heartbeat())

    # Idle watchdog: kills the stream only when the agent goes idle_timeout seconds
    # with zero activity. An actively working agent is never killed by this.
    async def _idle_watchdog():
        nonlocal _stream_task
        while True:
            await asyncio.sleep(10)
            idle_secs = time.monotonic() - _last_activity[0]
            if idle_secs >= idle_timeout:
                log.warning(
                    "master_agent.idle_timeout",
                    project=project.name,
                    idle_secs=int(idle_secs),
                    idle_timeout=idle_timeout,
                )
                if _stream_task and not _stream_task.done():
                    _stream_task.cancel()
                return

    _wd_task = asyncio.create_task(_idle_watchdog())

    async def _run_stream():
        nonlocal success, current_step, docker_fix_attempts
        async for message in query(prompt=prompt, options=options):
            if not isinstance(message, AssistantMessage):
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
                    _bump_activity()
                    # Stream raw agent text to web frontend in real-time
                    if block.text.strip() and hasattr(checklist._chat, "send_agent_token"):
                        try:
                            await checklist._chat.send_agent_token(
                                checklist._chat_id, checklist._message_id, block.text
                            )
                        except Exception:
                            pass
                    for line in block.text.split("\n"):
                        line = line.strip()
                        if not line:
                            continue

                        # STATUS: Show what agent is doing
                        if line.startswith("STATUS:"):
                            detail = line[7:].strip()[:60]
                            if current_step <= max_step:
                                await checklist.update_step(current_step, detail)

                        # DIAGNOSIS: Show failure analysis
                        elif line.startswith("DIAGNOSIS:"):
                            detail = line[10:].strip()[:80]
                            if current_step <= max_step:
                                await checklist.update_step(current_step, f"⚠️ {detail}")

                        # ACTION: Show what agent is fixing
                        elif line.startswith("ACTION:"):
                            detail = line[7:].strip()[:60]
                            if current_step <= max_step:
                                await checklist.update_step(current_step, f"🔧 {detail}")
                            # Track Docker fix attempts — break agent loop if exhausted
                            if current_step == 4:
                                docker_fix_attempts += 1
                                if docker_fix_attempts > max_docker_fixes:
                                    log.warning("bootstrap.docker_fixes_exhausted",
                                                project=project.name,
                                                attempts=docker_fix_attempts)
                                    success = False
                                    break
                        
                        # STEP_DONE: Complete a step and move to next
                        elif line.startswith("STEP_DONE:"):
                            parts = line[10:].strip().split(" ", 1)
                            try:
                                step_num = int(parts[0])
                            except ValueError:
                                step_num = current_step
                            detail = parts[1] if len(parts) > 1 else ""
                            if step_num <= max_step:
                                await checklist.complete_step(step_num, detail[:60])
                            current_step = step_num + 1
                            if current_step <= max_step:
                                await checklist.start_step(current_step)
                        
                        # STEP_SKIP: Skip a step
                        elif line.startswith("STEP_SKIP:"):
                            parts = line[10:].strip().split(" ", 1)
                            try:
                                step_num = int(parts[0])
                            except ValueError:
                                step_num = current_step
                            detail = parts[1] if len(parts) > 1 else "skipped"
                            if step_num <= max_step:
                                await checklist.skip_step(step_num, detail[:60])
                            current_step = step_num + 1
                            if current_step <= max_step:
                                await checklist.start_step(current_step)
                        
                        # STEP_FAIL: Mark step as failed
                        elif line.startswith("STEP_FAIL:"):
                            parts = line[10:].strip().split(" ", 1)
                            try:
                                step_num = int(parts[0])
                            except ValueError:
                                step_num = current_step
                            detail = parts[1] if len(parts) > 1 else "failed"
                            if step_num <= max_step:
                                await checklist.fail_step(step_num, detail[:60])
                        
                        # COMPLETE keyword
                        elif complete_keyword in line:
                            success = True
                            log.info("master_agent.complete", project=project.name)

                        # FAILED keyword
                        elif line.startswith(f"{failed_keyword}:"):
                            reason = line[len(failed_keyword)+1:].strip()[:100] or "agent failed"
                            log.error("bootstrap.master_agent_failed", 
                                      project=project.name, reason=reason)
                            success = False
                
                elif isinstance(block, ToolUseBlock):
                    _bump_activity()
                    # Show tool usage in checklist for transparency
                    if current_step <= max_step:
                        from openclow.worker.tasks._agent_base import describe_tool
                        desc = describe_tool(block)
                        await checklist.update_step(current_step, desc)

    # Run the stream as a Task so heartbeat/watchdog can cancel it mid-MCP-call.
    # No flat wall-clock timeout — the idle watchdog handles stuck agents instead.
    _stream_task = asyncio.create_task(_run_stream())
    try:
        await _stream_task
    except asyncio.CancelledError:
        idle_secs = int(time.monotonic() - _last_activity[0])
        if idle_secs >= idle_timeout:
            # Killed by idle watchdog — report as timeout
            log.warning("master_agent.idle_timeout_fired", project=project.name,
                        idle_secs=idle_secs)
            if current_step <= max_step:
                await checklist.fail_step(
                    current_step,
                    f"agent stuck (no activity for {idle_secs}s)",
                )
        else:
            # Cancelled by web Stop button or worker shutdown — propagate
            raise
    except Exception as e:
        log.error("bootstrap.master_agent_failed", error=str(e))
        if current_step <= max_step:
            await checklist.fail_step(current_step, str(e)[:60])
    finally:
        _hb_task.cancel()
        _wd_task.cancel()
        try:
            await asyncio.gather(_hb_task, _wd_task, return_exceptions=True)
        except Exception:
            pass

    return success


# ---------------------------------------------------------------------------
# Main bootstrap task
# ---------------------------------------------------------------------------

async def bootstrap_project(ctx: dict, project_id: int, chat_id: str, message_id: str, chat_provider_type: str = "telegram"):
    """Agentic project bootstrap with live checklist progress.

    Uses ChecklistReporter for UX and Claude Agent for smart setup.
    The LLM reads the project, plans steps, executes them, fixes errors.

    For mode="host" projects, dispatches to `_bootstrap_project_host` which
    works against an already-on-disk directory on the VPS host instead of
    spinning up Docker containers.
    """
    chat = await factory.get_chat_by_type(chat_provider_type)

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()

    if not project:
        await chat.send_error(chat_id, message_id, "Project not found")
        return

    # T088: bootstrap router flip — go-live for per-chat instances.
    # mode='container' projects don't use the legacy project-wide
    # bootstrap at all. Provisioning is per-chat via
    # InstanceService.get_or_resume (wired in assistant_endpoint).
    # The legacy host/docker code paths below remain UNCHANGED per
    # FR-034 (no edits to the legacy path).
    project_mode = getattr(project, "mode", "docker") or "docker"
    if project_mode == "container":
        # For web chats, chat_id shape is "web:<user_id>:<session_id>";
        # the session_id is the authoritative WebChatSession row and
        # lets InstanceService.get_or_resume pick up the thread.
        chat_session_id: int | None = None
        if chat_id.startswith("web:"):
            parts = chat_id.split(":")
            if len(parts) == 3 and parts[2].isdigit():
                chat_session_id = int(parts[2])
        if chat_session_id is None:
            await chat.edit_message(
                chat_id, message_id,
                "Container-mode projects are provisioned per-chat. Start "
                "a chat and send any message — your environment will come "
                "up automatically.",
            )
            return
        from openclow.services.instance_service import (
            InstanceService, PerUserCapExceeded, PlatformAtCapacity,
        )
        try:
            inst = await InstanceService().get_or_resume(
                chat_session_id=chat_session_id
            )
            await chat.edit_message(
                chat_id, message_id,
                f"Your environment is {inst.status}. It will be ready in "
                "about 90 seconds.",
            )
        except PerUserCapExceeded as e:
            await chat.edit_message(
                chat_id, message_id,
                f"You already have {len(e.active_chat_ids)} active chats "
                f"(cap={e.cap}). End one to start another.",
            )
        except PlatformAtCapacity:
            await chat.edit_message(
                chat_id, message_id,
                "The platform is at capacity right now. Please try again "
                "in a few minutes.",
            )
        return

    if project_mode == "host":
        return await _bootstrap_project_host(
            ctx, project, chat, chat_id, message_id,
        )

    from openclow.services.project_lock import acquire_project_lock, get_lock_holder
    lock = await acquire_project_lock(project_id, task_id=f"bootstrap-{project.name}", wait=5)
    if lock is None:
        holder = await get_lock_holder(project_id)
        await chat.edit_message(chat_id, message_id,
                                f"Cannot bootstrap — project is locked by task {holder}.\n"
                                f"Wait for it to finish or use /cancel.")
        await chat.close()
        return

    await _set_project_status(project_id, "bootstrapping")

    # Clear any stale cancel flag — a previous Stop click sets this key (600s TTL) and the
    # cancel heartbeat inside _run_master_agent would kill this bootstrap within 5 seconds.
    # Telegram/Slack don't have this mechanism; web does, so we must wipe it on fresh starts.
    if chat_id.startswith("web:"):
        try:
            _parts = chat_id.split(":")
            if len(_parts) == 3:
                import redis.asyncio as _aioredis
                _rc = _aioredis.from_url(settings.redis_url)
                await _rc.delete(f"openclow:cancel_session:{_parts[2]}")
                await _rc.aclose()
                log.info("bootstrap.cleared_cancel_flag", session=_parts[2])
        except Exception:
            pass

    workspace = os.path.join(settings.workspace_base_path, "_cache", project.name)
    compose = project.docker_compose_file or "docker-compose.yml"
    compose_project = f"openclow-{project.name}"

    # Use allocated port for this project (deterministic, unique per project)
    from openclow.services.port_allocator import get_app_port
    port = get_app_port(project_id)
    await _save_project_port(project_id, port)  # persist so tunnel health loop can find this project

    if not message_id or message_id == "0":
        message_id = await chat.send_message(chat_id, f"Setting up {project.name}...")
        message_id = str(message_id)

    # ── ALL steps shown upfront with progress bar ──
    checklist = ChecklistReporter(
        chat, chat_id, message_id,
        title=f"Setting up {project.name}",
        subtitle=project.tech_stack or "",
    )

    # ALL steps upfront — correct order: deps → build → docker → migrations → verify → tunnel
    ALL_STEPS = [
        "Clone repository",            # 0
        "Setup environment",            # 1
        "Install dependencies",         # 2
        "Build frontend assets",        # 3
        "Start Docker containers",      # 4
        "Run database migrations",      # 5
        "Verify app",                   # 6
        "Create public URL",            # 7  ← LAST — only after app works
    ]
    checklist.set_steps(ALL_STEPS)
    await checklist._force_render()
    await checklist.start()

    try:
        # ── Step 0: Clone ──
        await checklist.start_step(0)
        clone_detail = await _step_clone(
            lambda msg: checklist.update_step(0, msg), project, workspace,
        )
        await checklist.complete_step(0, clone_detail)

        # ── Step 1: Environment ──
        await checklist.start_step(1)
        env_detail = await _step_env(
            lambda msg: checklist.update_step(1, msg), workspace, compose,
        )
        await checklist.complete_step(1, env_detail)

        # ── Preflight: clean environment before agent starts ──
        await checklist.update_step(1, "preflight checks...")
        await _preflight(project, workspace, compose, compose_project)

        # ── Helper: bail on failure — clean up containers, skip remaining steps ──
        async def _bail(reason: str):
            """Clean up Docker containers, mark steps skipped, set failed status."""
            # CRITICAL: Stop containers so they don't pile up across retries
            from openclow.services.docker_guard import run_docker_compose
            try:
                await run_docker_compose(
                    "down", "--remove-orphans",
                    compose_file=compose, compose_project=compose_project,
                    actor="bootstrap", project_name=project.name,
                    cwd=workspace, timeout=30,
                )
            except Exception:
                pass
            for i, s in enumerate(checklist.steps):
                if s["status"] in ("pending", "running"):
                    await checklist.skip_step(i, "skipped")
            await _set_project_status(project_id, "failed")
            checklist._footer = f"❌ {reason}"
            from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
            kb = ActionKeyboard(rows=[
                ActionRow([ActionButton("🔄 Retry Bootstrap", f"project_bootstrap:{project.id}")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ])
            await checklist.stop()
            await checklist._force_render(keyboard=kb)

        # ── Steps 2-6: Master Agent handles everything with unified reasoning ──
        # Retry up to 2 times — the SDK sometimes crashes on non-zero exit codes
        # but the agent can continue from where it left off on retry
        await checklist.start_step(2)
        master_success = False
        max_agent_attempts = 2

        # Background heartbeat — sends elapsed time every 30s during agent run
        # so the UI progress bar never looks frozen during long Docker tool calls
        _heartbeat_stop = asyncio.Event()
        async def _heartbeat():
            elapsed = 0
            while not _heartbeat_stop.is_set():
                await asyncio.sleep(30)
                elapsed += 30
                # Find the current running step
                current = next(
                    (s["name"] for s in checklist.steps if s["status"] == "running"),
                    "working...",
                )
                try:
                    running_idx = next(
                        i for i, s in enumerate(checklist.steps) if s["status"] == "running"
                    )
                    await checklist.update_step(running_idx, f"{current} — {elapsed}s elapsed...")
                except Exception:
                    pass
        _heartbeat_task = asyncio.ensure_future(_heartbeat())

        for attempt in range(1, max_agent_attempts + 1):
            try:
                master_success = await _run_master_agent(
                    checklist, project, workspace, compose, compose_project, port,
                )
                break  # Success or graceful failure — don't retry
            except asyncio.CancelledError:
                _heartbeat_stop.set()
                _heartbeat_task.cancel()
                log.info("bootstrap.cancelled_by_user", project=project.name)
                await _bail("Bootstrap cancelled by user")
                return
            except Exception as e:
                log.warning("bootstrap.agent_attempt_failed",
                            project=project.name, attempt=attempt, error=str(e)[:200])
                if attempt < max_agent_attempts:
                    # Find first incomplete step for the retry
                    resume_step = 2
                    for i in range(2, 7):
                        if checklist.steps[i]["status"] in ("pending", "running"):
                            resume_step = i
                            break
                    await checklist.update_step(resume_step, f"retrying (attempt {attempt + 1})...")
                else:
                    _heartbeat_stop.set()
                    _heartbeat_task.cancel()
                    await _bail(f"Agent error after {attempt} attempts: {str(e)[:150]}")
                    return

        _heartbeat_stop.set()
        _heartbeat_task.cancel()

        # Validate results — trust the agent, verify with real checks
        if master_success:
            # Agent said BOOTSTRAP_COMPLETE — trust it.
            # Mark any steps still "running" as done (agent completed but didn't emit marker)
            for step_idx in [2, 3, 4, 5, 6]:
                if checklist.steps[step_idx]["status"] == "running":
                    await checklist.complete_step(step_idx, "completed")
        else:
            # Agent failed — mark running steps as incomplete
            for step_idx in [2, 3, 4, 5, 6]:
                if checklist.steps[step_idx]["status"] == "running":
                    await checklist.fail_step(step_idx, "incomplete")

            # Real verification: are containers up and app responding?
            # If yes, mark as success despite agent text parsing issues
            from openclow.services.docker_guard import run_docker
            app_name = project.app_container_name or "app"
            rc, status_out = await run_docker(
                "docker", "inspect", f"{compose_project}-{app_name}-1",
                "--format", "{{.State.Status}}", actor="bootstrap",
            )
            if rc == 0 and "running" in status_out:
                log.info("bootstrap.real_verify_passed", project=project.name)
                master_success = True
                for step_idx in [2, 3, 4, 5, 6]:
                    if checklist.steps[step_idx]["status"] in ("failed", "pending"):
                        await checklist.complete_step(step_idx, "verified running")

        if not master_success:
            # Find first failed step for error message
            failed_step = None
            for i in [2, 3, 4, 5, 6]:
                if checklist.steps[i]["status"] == "failed":
                    failed_step = ALL_STEPS[i]
                    break
            reason = f"{failed_step} failed" if failed_step else "Setup failed"
            await _bail(f"{reason} — check diagnosis above")
            return
        
        # Determine if app is OK based on step 6 status
        app_ok = checklist.steps[6]["status"] == "done"

        # ── Step 7: Create public URL (ONLY if verify passed) ──
        if not app_ok:
            await _set_project_status(project_id, "failed")
            await checklist.fail_step(7, "skipped — app not verified")
            checklist._footer = "⚠️ App not responding — tunnel not created"
            from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
            kb = ActionKeyboard(rows=[
                ActionRow([ActionButton("🔄 Retry Bootstrap", f"project_bootstrap:{project.id}")]),
                ActionRow([ActionButton("💚 Health Check", f"health:{project.id}")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ])
            await checklist.stop()
            await checklist._force_render(keyboard=kb)
            log.warning("bootstrap.no_tunnel", project=project.name, reason="app_not_responding")
            return

        await checklist.start_step(7)
        from openclow.services.tunnel_service import start_tunnel
        tunnel_url = ""
        try:
            # Get tunnel target via container IP (not localhost — worker can't reach host ports)
            tunnel_target = await _get_tunnel_target(compose_project, workspace, project.id)
            if not tunnel_target:
                # Retry after a short wait — container may still be starting
                import asyncio as _aio
                await _aio.sleep(5)
                tunnel_target = await _get_tunnel_target(compose_project, workspace, project.id)
            if not tunnel_target:
                log.warning("bootstrap.tunnel_target_not_found", project=compose_project)
                raise RuntimeError("Could not find app container IP — containers may not be running")

            # Detect host header from APP_URL (e.g. "abc.test" for virtual host matching)
            host_header = None
            env_path = os.path.join(workspace, ".env")
            if os.path.exists(env_path):
                with open(env_path) as f:
                    for line in f:
                        if line.strip().startswith("APP_URL="):
                            app_url = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                            # Extract hostname: http://abc.test → abc.test
                            from urllib.parse import urlparse
                            parsed = urlparse(app_url)
                            if parsed.hostname and parsed.hostname not in ("localhost", "127.0.0.1"):
                                host_header = parsed.hostname
                            break

            await checklist.update_step(7, f"creating tunnel...")
            url = await start_tunnel(project.name, tunnel_target, host_header=host_header)
            if url:
                # Verify the tunnel URL is actually reachable before proceeding
                from openclow.services.tunnel_service import verify_tunnel_url, stop_tunnel
                await checklist.update_step(7, "verifying tunnel...")
                if not await verify_tunnel_url(url):
                    # First attempt failed — stop, wait, retry once
                    log.warning("bootstrap.tunnel_verify_failed", url=url)
                    await stop_tunnel(project.name)
                    await asyncio.sleep(3)
                    url = await start_tunnel(project.name, tunnel_target, host_header=host_header)
                    if url and not await verify_tunnel_url(url):
                        log.warning("bootstrap.tunnel_verify_failed_retry", url=url)
                        url = None  # give up — report tunnel as failed

                if not url:
                    await checklist.fail_step(7, "tunnel not reachable after retry")
                    # Continue to final summary — tunnel failure shouldn't block the rest
                else:
                    tunnel_url = url
                    # Configure app .env to use tunnel URL (fix mixed content, CORS, etc.)
                    await _configure_app_for_tunnel(workspace, url, compose_project)
                    await checklist.update_step(7, "rebuilding assets with tunnel URL...")
                    # Agent rebuilds frontend + clears caches via MCP docker_exec
                    try:
                        from claude_agent_sdk import query, ClaudeAgentOptions
                        from openclow.providers.llm.claude import _mcp_docker
                        post_opts = ClaudeAgentOptions(
                            cwd=workspace,
                            system_prompt="DevOps engineer. Rebuild frontend assets and clear caches after tunnel URL change. Use Docker MCP tools only." + _NO_DOCKER_SOCKET,
                            model="claude-sonnet-4-6",
                            allowed_tools=[
                                "Read", "Glob", "Grep",
                                "mcp__docker__docker_exec",
                                "mcp__docker__compose_ps",
                                "mcp__docker__list_containers",
                                "mcp__docker__container_logs",
                            ],
                            mcp_servers={"docker": _mcp_docker()},
                            permission_mode="bypassPermissions",
                            max_turns=10,
                        )
                        post_prompt = (
                            f"The tunnel URL for project '{project.name}' was just set to: {url}\n"
                            f"The .env at {workspace}/.env has been updated. ASSET_URL is empty (relative paths)\n"
                            f"so assets load correctly from both localhost and the tunnel URL (no CORS issues).\n"
                            f"Compose project: {compose_project}\n\n"
                            f"DO THIS:\n"
                            f"1. compose_ps(\"{compose_project}\") — find the app container\n"
                            f"2. docker_exec into the app container to rebuild frontend assets:\n"
                            f"   - Find workdir: docker_exec(container, 'pwd') then docker_exec(container, 'ls')\n"
                            f"   - Try in order: npm run build, npx vite build, ./node_modules/.bin/vite build\n"
                            f"   - If node/npm not in PATH, look for it: find / -name node -type f 2>/dev/null | head -3\n"
                            f"   - If no node at all in the container, skip frontend rebuild\n"
                            f"3. Clear framework caches via docker_exec:\n"
                            f"   - Laravel: php artisan config:clear && php artisan cache:clear && php artisan view:clear\n"
                            f"   - Django: python manage.py clear_cache\n"
                            f"   - Skip if no framework detected\n"
                            f"4. NEVER modify project source code (*.php, *.js, etc). Only .env and infra config.\n"
                            f"5. Output DONE when finished\n"
                        )
                        async for msg in query(prompt=post_prompt, options=post_opts):
                            pass
                    except Exception as e:
                        log.warning("bootstrap.post_tunnel_agent_failed", error=str(e))
                    await checklist.complete_step(7, url[:50])
            else:
                await checklist.fail_step(7, "tunnel failed to start")
        except Exception as e:
            await checklist.fail_step(7, str(e)[:40])

        # ── Final summary with buttons ──
        checklist._footer = ""
        all_done = all(s["status"] in ("done", "skipped") for s in checklist.steps)
        any_failed = any(s["status"] == "failed" for s in checklist.steps)

        # Update project status based on outcome
        if any_failed:
            await _set_project_status(project_id, "failed")
        else:
            await _set_project_status(project_id, "active")

        if all_done:
            checklist._footer = "🚀 Ready for tasks!"
        elif any_failed:
            checklist._footer = "⚠️ Some steps had issues — check above"
        else:
            checklist._footer = "✅ Setup complete"

        if tunnel_url:
            checklist._footer += f"\n🌐 {tunnel_url}"

        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        rows = []
        from openclow.providers.actions import open_app_btns
        rows.append(ActionRow(open_app_btns(project.id, tunnel_url=tunnel_url)))
        rows.append(ActionRow([
            ActionButton("🚀 New Task", "menu:task"),
            ActionButton("💚 Health", f"health:{project.id}"),
        ]))

        await checklist.stop()
        await checklist._force_render(keyboard=ActionKeyboard(rows=rows))

        log.info("bootstrap.complete", project=project.name, all_done=all_done)

    except (Exception, asyncio.CancelledError, TimeoutError) as e:
        await _set_project_status(project_id, "failed")
        # Clean up containers + tunnel on crash — don't leave orphans
        try:
            from openclow.services.docker_guard import run_docker_compose
            await run_docker_compose(
                "down", "--remove-orphans",
                compose_file=compose, compose_project=compose_project,
                actor="bootstrap", project_name=project.name,
                cwd=workspace, timeout=30,
            )
        except Exception:
            pass
        try:
            from openclow.services.tunnel_service import stop_tunnel
            await stop_tunnel(project.name)
        except Exception:
            pass
        await checklist.stop()
        error_msg = "Job timed out" if isinstance(e, (asyncio.CancelledError, TimeoutError)) else str(e)[:150]
        log.error("bootstrap.failed", project=project.name, error=error_msg)
        checklist._footer = f"❌ {error_msg}"
        try:
            from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
            await checklist._force_render(keyboard=ActionKeyboard(rows=[
                ActionRow([ActionButton("🔄 Retry Bootstrap", f"project_bootstrap:{project.id}")]),
                ActionRow([
                    ActionButton("🔗 Unlink", f"project_unlink:{project.id}"),
                    ActionButton("🗑 Remove", f"project_remove:{project.id}", style="danger"),
                ]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ]))
        except Exception:
            pass
    finally:
        if lock:
            await lock.release()
        from openclow.services.audit_service import flush
        await flush()
        await chat.close()


# ===========================================================================
# Host-mode bootstrap: mode="host" projects (already-running app on VPS host)
# ===========================================================================

HOST_MASTER_BOOTSTRAP_PROMPT = """You are a senior DevOps engineer bringing up a project on a VPS host.

PROJECT: {project_name}
TECH STACK: {tech_stack}
PROJECT DIR: {project_dir}
INSTALL GUIDE: {install_guide_path}
START COMMAND: {start_command}
APP PORT: {app_port}
HEALTH URL: {health_url}
PROCESS MANAGER: {process_manager}

INSTALL GUIDE CONTENTS (first 4KB):
---
{install_guide_body}
---

MISSION — execute the steps below IN ORDER. Stream narration with STATUS:, DIAGNOSIS:,
ACTION: and STEP_DONE: N <summary> lines so the user can follow what you're doing.

RULES:
- MCP-first: always attempt with the host_* tools before reasoning alone. Available host
  tools: host_cd, host_git_pull, host_read_install_guide, host_run_command (allowlisted
  shell), host_check_port, host_curl, host_process_status, host_tail_log, host_start_app,
  host_stop_app, host_service_status.
- Work INSIDE the project dir. Every host_run_command call takes project_dir as its cwd.
- Read the install guide carefully and follow the commands it specifies — do not invent
  a different install path unless the guide's commands fail.
- Narrate before AND after each tool call. Before: one sentence on what you're about to
  do. After: 2-3 sentences on what actually happened.
- NEVER GIVE UP. If a command fails, read the output, form a concrete hypothesis, try a
  DIFFERENT approach. "The tool kept failing" is not a reason — understand WHY and fix
  THAT first. Document at least three concretely-different attempts before marking
  STEP_FAIL.

STEPS:

STEP 1 — Sync code
  host_git_pull("{project_dir}")
  STEP_DONE: 1 <short git log line>

STEP 2 — Read the install guide
  host_read_install_guide("{project_dir}")
  STEP_DONE: 2 <guide summary in ~20 words>

STEP 3 — Run setup commands (install deps, migrate, etc.)
  Use host_run_command for each command from the guide in order.
  If a command fails, diagnose (missing binary? wrong dir? permissions?) and try a
  different approach.
  STEP_DONE: 3 <what you installed / configured>

STEP 4 — Ensure the app is running
  host_process_status("{project_name}") — see if it's already up.
  If not: host_start_app("{project_dir}", "{start_command}")
  STEP_DONE: 4 <PID + what you started>

STEP 5 — Health check
  host_curl("{health_url}") — expect 2xx/3xx.
  If failed: host_tail_log (check .openclow-start.log and any app log), diagnose, fix,
  restart via host_stop_app + host_start_app, re-verify.
  STEP_DONE: 5 HTTP <code>

STEP 6 — Done
  Output the marker: BOOTSTRAP_COMPLETE

If you cannot get past a step after three concrete attempts, output:
  BOOTSTRAP_FAILED: <step number> — <exact reason + what each attempt returned>
"""


async def _bootstrap_project_host(
    ctx: dict, project, chat, chat_id: str, message_id: str,
):
    """Host-mode bootstrap. Uses host_* MCP tools instead of Docker compose.
    Tunnel still runs via tunnel_service (targets http://host.docker.internal:PORT
    from inside the worker container — the same address works both in the local
    simulation and on a production VPS where docker-compose adds the
    host-gateway extra_host)."""
    import asyncio
    import os

    from openclow.services.checklist_reporter import ChecklistReporter
    from openclow.services.host_guard import run_host
    from openclow.services.project_lock import acquire_project_lock, get_lock_holder

    project_id = project.id
    lock = await acquire_project_lock(project_id, task_id=f"bootstrap-{project.name}", wait=5)
    if lock is None:
        holder = await get_lock_holder(project_id)
        await chat.edit_message(
            chat_id, message_id,
            f"Cannot bootstrap — project is locked by task {holder}.",
        )
        await chat.close()
        return

    await _set_project_status(project_id, "bootstrapping")

    if not project.project_dir:
        await chat.edit_message(chat_id, message_id,
                                f"Project {project.name} has no project_dir set.")
        await _set_project_status(project_id, "failed")
        if lock:
            await lock.release()
        return

    if not os.path.isdir(project.project_dir):
        await chat.edit_message(chat_id, message_id,
                                f"Project dir {project.project_dir} does not exist on the host.")
        await _set_project_status(project_id, "failed")
        if lock:
            await lock.release()
        return

    if not message_id or message_id == "0":
        message_id = await chat.send_message(chat_id, f"Setting up {project.name}...")
        message_id = str(message_id)

    checklist = ChecklistReporter(
        chat, chat_id, message_id,
        title=f"Setting up {project.name} (host mode)",
        subtitle=project.tech_stack or "",
    )
    HOST_STEPS = [
        "Sync code",
        "Read install guide",
        "Install dependencies",
        "Start app",
        "Health check",
        "Create public URL",
    ]
    checklist.set_steps(HOST_STEPS)
    await checklist._force_render()
    await checklist.start()

    tunnel_url = ""
    master_success = False

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
        from openclow.providers.llm.claude import _mcp_host

        # Load install guide contents for the prompt
        install_body = ""
        if project.install_guide_path:
            guide_full = os.path.join(project.project_dir, project.install_guide_path)
            if os.path.isfile(guide_full):
                try:
                    with open(guide_full, encoding="utf-8", errors="replace") as f:
                        install_body = f.read(4096)
                except Exception:
                    install_body = ""

        health_url = project.health_url or (
            f"http://localhost:{project.app_port}/" if project.app_port else ""
        )

        prompt = HOST_MASTER_BOOTSTRAP_PROMPT.format(
            project_name=project.name,
            tech_stack=project.tech_stack or "unknown",
            project_dir=project.project_dir,
            install_guide_path=project.install_guide_path or "(none)",
            start_command=project.start_command or "(not set — infer from the install guide)",
            app_port=project.app_port or "(not set)",
            health_url=health_url or "(not set)",
            process_manager=project.process_manager or "manual",
            install_guide_body=install_body or "(install guide not found)",
        )

        # Optional Redis stream channel for tool-output streaming to the web UI.
        env = dict(os.environ)
        if chat_id.startswith("web:"):
            _parts = chat_id.split(":")
            if len(_parts) == 3:
                env["HOST_STREAM_CHANNEL"] = f"wc:{_parts[1]}:{_parts[2]}"

        mcp_host_def = _mcp_host()
        mcp_host_def["env"] = env  # propagate HOST_STREAM_CHANNEL to the MCP subprocess

        options = ClaudeAgentOptions(
            cwd=project.project_dir,
            system_prompt=(
                "You are a Senior DevOps Engineer and AI Chat Support Engineer. "
                "Use the host_* MCP tools to install and run an already-on-disk project. "
                "Stream narration before and after every tool call. Never give up."
            ),
            model="claude-sonnet-4-6",
            allowed_tools=[
                "Read", "Glob", "Grep",
                "mcp__host__host_cd",
                "mcp__host__host_git_pull",
                "mcp__host__host_read_install_guide",
                "mcp__host__host_run_command",
                "mcp__host__host_check_port",
                "mcp__host__host_curl",
                "mcp__host__host_process_status",
                "mcp__host__host_tail_log",
                "mcp__host__host_start_app",
                "mcp__host__host_stop_app",
                "mcp__host__host_service_status",
            ],
            mcp_servers={"host": mcp_host_def},
            permission_mode="bypassPermissions",
            max_turns=40,
        )

        await checklist.start_step(0)

        full_output = ""
        last_step = -1
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_output += block.text
                        # Advance checklist on STEP_DONE: N markers
                        for m in re.finditer(r"STEP_DONE:\s*(\d+)\s*(.*)", block.text):
                            n = int(m.group(1))
                            detail = m.group(2).strip()[:60]
                            if 1 <= n <= 5 and n - 1 > last_step:
                                # Fill in any skipped steps as completed too
                                for i in range(last_step + 1, n):
                                    if checklist.steps[i]["status"] != "done":
                                        await checklist.complete_step(i, "ok")
                                await checklist.complete_step(n - 1, detail or "ok")
                                last_step = n - 1
                                if n < 5:
                                    await checklist.start_step(n)
                    elif isinstance(block, ToolUseBlock):
                        try:
                            from openclow.worker.tasks._agent_base import describe_tool
                            desc = describe_tool(block)
                            running_idx = next(
                                (i for i, s in enumerate(checklist.steps)
                                 if s["status"] == "running"),
                                last_step + 1,
                            )
                            await checklist.update_step(
                                max(0, min(running_idx, 4)), desc[:80],
                            )
                        except Exception:
                            pass

        if "BOOTSTRAP_COMPLETE" in full_output:
            master_success = True
            for i in range(5):
                if checklist.steps[i]["status"] != "done":
                    await checklist.complete_step(i, "completed")
        elif "BOOTSTRAP_FAILED" in full_output:
            # find running and mark as failed with extracted reason
            reason_m = re.search(r"BOOTSTRAP_FAILED:\s*\d+\s*[—-]\s*(.+)", full_output)
            reason = (reason_m.group(1) if reason_m else "agent reported failure").strip()[:150]
            for i in range(5):
                if checklist.steps[i]["status"] == "running":
                    await checklist.fail_step(i, reason[:40])
            checklist._footer = f"❌ {reason}"
        else:
            # Verify by curl regardless — agent may have stopped emitting markers
            if health_url:
                rc, out = await run_host(
                    f"curl -sS -o /dev/null -w '%{{http_code}}' --max-time 5 {health_url}",
                    cwd=project.project_dir, actor="bootstrap", timeout=8,
                    project_name=project.name, project_id=project.id,
                )
                if rc == 0 and out.strip() and out.strip()[0] in "23":
                    master_success = True
                    for i in range(5):
                        if checklist.steps[i]["status"] != "done":
                            await checklist.complete_step(i, "verified running")

        if not master_success:
            await _set_project_status(project_id, "failed")
            from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
            await checklist.stop()
            await checklist._force_render(keyboard=ActionKeyboard(rows=[
                ActionRow([ActionButton("🔄 Retry", f"project_bootstrap:{project.id}")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ]))
            log.warning("bootstrap.host_failed", project=project.name)
            return

        # ── Step 6 (index 5): public URL ──
        # If the project has its own domain (nginx + owned domain on the VPS),
        # skip cloudflared entirely — just return the configured public_url.
        # Otherwise fall back to spinning up a cloudflared tunnel.
        await checklist.start_step(5)
        tunnel_enabled = getattr(project, "tunnel_enabled", True)
        configured_url = getattr(project, "public_url", None)
        if configured_url and not tunnel_enabled:
            tunnel_url = configured_url
            await checklist.complete_step(5, f"domain: {configured_url[:48]}")
        elif project.app_port:
            tunnel_target = f"http://host.docker.internal:{project.app_port}"
            try:
                from openclow.services.tunnel_service import start_tunnel, stop_tunnel, verify_tunnel_url
                url = await start_tunnel(project.name, tunnel_target)
                if url:
                    if not await verify_tunnel_url(url):
                        await stop_tunnel(project.name)
                        await asyncio.sleep(3)
                        url = await start_tunnel(project.name, tunnel_target)
                if url:
                    tunnel_url = url
                    await checklist.complete_step(5, url[:50])
                else:
                    await checklist.fail_step(5, "tunnel failed")
            except Exception as e:
                await checklist.fail_step(5, str(e)[:40])
        else:
            await checklist.fail_step(5, "no app_port set")

        await _set_project_status(project_id, "active")

        checklist._footer = "🚀 Host-mode project ready!"
        if tunnel_url:
            checklist._footer += f"\n🌐 {tunnel_url}"

        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow, open_app_btns
        rows = [
            ActionRow(open_app_btns(project.id, tunnel_url=tunnel_url)),
            ActionRow([
                ActionButton("🚀 New Task", "menu:task"),
                ActionButton("💚 Health", f"health:{project.id}"),
            ]),
        ]
        await checklist.stop()
        await checklist._force_render(keyboard=ActionKeyboard(rows=rows))
        log.info("bootstrap.host_complete", project=project.name, tunnel=tunnel_url)

    except (Exception, asyncio.CancelledError, TimeoutError) as e:
        await _set_project_status(project_id, "failed")
        await checklist.stop()
        error_msg = "Cancelled" if isinstance(e, (asyncio.CancelledError, TimeoutError)) else str(e)[:150]
        checklist._footer = f"❌ {error_msg}"
        log.error("bootstrap.host_failed", project=project.name, error=error_msg)
        try:
            from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
            await checklist._force_render(keyboard=ActionKeyboard(rows=[
                ActionRow([ActionButton("🔄 Retry", f"project_bootstrap:{project.id}")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ]))
        except Exception:
            pass
    finally:
        if lock:
            await lock.release()
        from openclow.services.audit_service import flush
        await flush()
        await chat.close()


