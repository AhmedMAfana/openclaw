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

from sqlalchemy import select

from openclow.models import Project, async_session
from openclow.providers import factory
from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()


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
- Read the project to understand the package manager (composer, npm, pip, etc.)
- For DOCKERIZED projects: usually SKIP — Docker handles deps at build time
- For non-dockerized: run the install command
- Verify deps exist (vendor/, node_modules/, .venv/, etc.)
- Output: STEP_DONE: 2 <short result> OR STEP_SKIP: 2 <reason>

STEP 3 — BUILD FRONTEND:
- If package.json exists with a build script, check if assets already compiled
- If the Dockerfile or docker-compose handles the build: SKIP
- If no frontend or assets already built: SKIP
- ONLY build on host if no Docker build step AND no compiled assets exist
- Output: STEP_DONE: 3 <short result> OR STEP_SKIP: 3 <reason>

STEP 4 — START DOCKER CONTAINERS:
- Use compose_up(build=True) for the first attempt — it handles build + start automatically.
  compose_up with build=True runs 'compose build' then 'compose up -d' as separate steps,
  which correctly handles Docker-in-Docker path translation (build contexts use container
  paths, volume mounts use host paths).
- You also have compose_build() if you need to build images separately before starting.
- If it FAILS — THIS IS CRITICAL:
  * Read the error output carefully
  * DIAGNOSE the root cause (missing env vars? ARM image issue? port conflict? build failure?)
  * Output DIAGNOSIS: <your analysis>
  * FIX IT (edit docker-compose.yml, .env, Dockerfile — whatever is needed)
  * Output ACTION: <what you're fixing>
  * Retry compose_up (or compose_build then compose_up if the build step is what failed)
  * You get up to 3 fix attempts
- After containers start, verify ALL are running via Docker MCP list_containers
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

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
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


async def _run_shell(cmd: str, cwd: str = None, timeout: int = 300) -> tuple[int, str]:
    """Run a shell command string."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        from openclow.services.config_service import get_config
        config = await get_config("git", "provider")
        if config and config.get("token"):
            env["GH_TOKEN"] = config["token"]
            env["GITHUB_TOKEN"] = config["token"]
    except Exception:
        pass

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        combined = (stdout.decode() + stderr.decode()).strip()
        return proc.returncode, combined[-4000:]
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, f"TIMEOUT after {timeout}s"
    except Exception as e:
        return -1, str(e)


# ---------------------------------------------------------------------------
# Telegram reporters (shared services)
# ---------------------------------------------------------------------------

from openclow.services.checklist_reporter import ChecklistReporter  # noqa: E402
from openclow.services.status_reporter import LineReporter as StatusReporter  # noqa: E402


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
    import subprocess

    # 1. Verify Docker daemon is accessible
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            log.error("preflight.docker_unavailable", stderr=result.stderr[:200])
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
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=openclow-{project.name}-",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
        orphans = set()
        for name in result.stdout.strip().split("\n"):
            name = name.strip()
            if not name:
                continue
            # Extract compose project from container name (e.g. openclow-tagh-fre-abc12345-app-1 → openclow-tagh-fre-abc12345)
            parts = name.rsplit("-", 1)  # Remove service suffix (-1)
            if len(parts) == 2:
                stack = parts[0].rsplit("-", 1)[0]  # Remove service name
                if stack != compose_project and stack.startswith(f"openclow-{project.name}"):
                    orphans.add(stack)
        for orphan in orphans:
            log.warning("preflight.cleaning_orphan", stack=orphan)
            subprocess.run(
                ["docker", "compose", "-p", orphan, "down", "--remove-orphans"],
                capture_output=True, timeout=30,
            )
    except Exception:
        pass

    # 4. Prune dangling networks (leftover from failed compose down)
    try:
        subprocess.run(
            ["docker", "network", "prune", "-f"],
            capture_output=True, timeout=10,
        )
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
        lines.append("\n# OpenClow port isolation (auto-generated)")
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
            result = subprocess.run(
                ["docker", "ps", "--filter", f"publish={app_port}", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=5,
            )
            for container in result.stdout.strip().split("\n"):
                container = container.strip()
                if container and container != f"{compose_project}-{project.app_container_name or 'app'}-1":
                    subprocess.run(
                        ["docker", "stop", container],
                        capture_output=True, timeout=10,
                    )
                    log.info("preflight.stopped_port_conflict", container=container, port=app_port)
        except Exception:
            pass

    # 9. Verify compose file exists
    compose_path = os.path.join(workspace, compose)
    if not os.path.exists(compose_path):
        raise RuntimeError(f"Compose file not found: {compose_path}")

    log.info("preflight.done", project=project.name, host_path=host_path,
             ports=f"app={app_port}")


async def _step_docker_up(
    checklist: ChecklistReporter, project, workspace: str, compose: str, compose_project: str,
) -> bool:
    """Step 4: Start Docker containers — generic for ANY project.

    Python-driven with live polling:
    1. docker compose up -d (through docker_guard for host path + ports)
    2. Poll every 5s for 90s — show "X/Y running"
    3. On failure — extract clean error per container from logs
    4. If stuck — call Doctor agent to diagnose
    No hardcoded container names or tech stack assumptions.

    ⚠️ LEGACY FUNCTION — Use _run_master_agent() instead.
    This function is kept for backward compatibility and may still be used by
    health_task.py for periodic repairs. The main bootstrap flow now uses the
    master agent which handles Docker operations inline with self-healing.

    Alternative: _run_master_agent(checklist, project, workspace, compose, compose_project, port)
    """
    log.warning("bootstrap.legacy_function_called", function="_step_docker_up",
                message="This function is deprecated for bootstrap. Use _run_master_agent() instead.")
    import json as _json

    await checklist.start_step(4)
    await checklist.update_step(4, "starting containers...")

    # 1. Get list of services first
    rc_cfg, cfg_output = await _run(
        "docker", "compose", "-f", compose, "-p", compose_project, "config", "--services",
        cwd=workspace, project_id=project.id,
    )
    all_services = [s.strip() for s in cfg_output.strip().split("\n") if s.strip()] if rc_cfg == 0 else []
    total_expected = len(all_services) or "?"
    await checklist.update_step(4, f"starting {total_expected} services...")

    # Run compose up through docker_guard (host path + port injection)
    rc, output = await _run(
        "docker", "compose", "-f", compose, "-p", compose_project, "up", "-d",
        cwd=workspace, timeout=600, project_id=project.id,
    )

    # rc != 0 doesn't always mean total failure — some containers may have started
    # while others failed (e.g. selenium has no ARM image). Check actual state.
    if rc != 0:
        key_error = _parse_docker_error(output)
        log.warning("bootstrap.compose_up_failed", error=key_error)

        # Let the agent diagnose and fix ANY docker compose failure
        await checklist.update_step(4, "agent diagnosing docker failure...")
        fixed = await _agent_fix_docker_config(
            workspace, compose, compose_project, project, output,
            checklist=checklist, step_idx=4,
        )
        if fixed:
            # Retry compose up after agent fix
            rc, output = await _run(
                "docker", "compose", "-f", compose, "-p", compose_project, "up", "-d",
                cwd=workspace, timeout=600, project_id=project.id,
            )
            if rc != 0:
                log.warning("bootstrap.compose_up_retry_failed", error=_parse_docker_error(output))
                await checklist.update_step(4, "checking what started...")
        else:
            log.warning("bootstrap.agent_fix_docker_failed")
            await checklist.update_step(4, "checking what started...")

    # 2. Poll containers until all running or timeout
    total_services = 0
    max_wait = 90
    poll_interval = 5

    for elapsed in range(0, max_wait, poll_interval):
        await asyncio.sleep(poll_interval)

        containers = await _get_compose_containers(compose_project, workspace, project.id)
        if not containers:
            await checklist.update_step(4, "waiting for containers...")
            continue

        total_services = len(containers)
        running = [c for c in containers if c["state"] == "running"]
        exited = [c for c in containers if c["state"] == "exited"]
        starting = [c for c in containers if c["state"] not in ("running", "exited")]

        # Update progress
        if starting:
            waiting = ", ".join(c["name"] for c in starting[:3])
            await checklist.update_step(4, f"{len(running)}/{total_services} running ({waiting}...)")
        else:
            await checklist.update_step(4, f"{len(running)}/{total_services} running")

        # All done (running or exited)?
        if len(running) + len(exited) >= total_services:
            break

    # 3. Final check
    containers = await _get_compose_containers(compose_project, workspace, project.id)
    if not containers:
        await checklist.fail_step(4, "no containers found after compose up")
        return False

    running = [c for c in containers if c["state"] == "running"]
    failed = [c for c in containers if c["state"] != "running"]

    if failed:
        # Get error details for each failed container
        error_details = []
        for c in failed[:3]:
            err = await _get_container_error(c["full_name"])
            error_details.append(f"{c['name']}: {err}")

        # Check if the APP container is running (the one with a web port)
        app_info = await _find_app_container(compose_project, workspace, project.id)
        app_is_running = app_info is not None

        if app_is_running:
            # App container is up — non-critical services failed (selenium, etc.)
            detail = f"{len(running)}/{total_services} up ({len(failed)} skipped)"
            await checklist.complete_step(4, detail[:50])
            checklist._footer = "Skipped: " + ", ".join(c["name"] for c in failed)
            await checklist._force_render()
            return True
        else:
            # App container is down — real failure
            detail = f"{len(running)}/{total_services} up"
            await checklist.fail_step(4, detail)
            checklist._footer = "Errors:\n" + "\n".join(f"  ❌ {e}" for e in error_details)
            await checklist._force_render()
            return False

    await checklist.complete_step(4, f"{len(running)}/{total_services} running")
    return True


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
    url_vars = {
        "APP_URL": tunnel_url,
        "ASSET_URL": tunnel_url,
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


async def _step_agentic_setup(
    checklist: ChecklistReporter, project, workspace: str, compose: str, compose_project: str,
) -> bool:
    """Steps 2-3: Agent handles deps + build only.
    Docker (step 4) and migrations (step 5) are handled by the caller.

    The agent outputs DONE: 1 (deps), DONE: 2 (build).

    ⚠️ LEGACY FUNCTION — Use _run_master_agent() instead.
    This function is kept for backward compatibility but is no longer used
    in the main bootstrap flow. The master agent handles steps 2-6 with
    unified reasoning and self-healing capabilities.

    Alternative: _run_master_agent(checklist, project, workspace, compose, compose_project, port)
    """
    log.warning("bootstrap.legacy_function_called", function="_step_agentic_setup",
                message="This function is deprecated. Use _run_master_agent() instead.")
    STEP_MAP = {1: 2, 2: 3}  # agent step → checklist index (only deps + build)

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
    except ImportError:
        await checklist.skip_step(2, "no SDK")
        await checklist.skip_step(3, "no SDK")
        return True

    await checklist.start_step(2)

    is_dockerized = getattr(project, "is_dockerized", True)

    if is_dockerized:
        prompt = f"""Set up "{project.name}" for development. Tech: {project.tech_stack or 'unknown'}.
This is a DOCKERIZED project — dependencies and builds happen INSIDE Docker containers.

YOU MUST DO EXACTLY 2 STEPS:

STEP 1 — INSTALL DEPENDENCIES:
- This is a dockerized project. Check if a Dockerfile or docker-compose.yml handles deps (composer install, npm install, pip install etc.)
- If the Dockerfile installs deps during build: SKIP: 1 handled by Docker
- ONLY install deps on the host if there is NO Docker-based build AND deps are missing
- If auth.json exists in workspace root, copy it to ~/.composer/auth.json before Docker build
- Output: DONE: 1 <short result> OR SKIP: 1 <reason>

STEP 2 — BUILD FRONTEND:
- If public/build/ already has compiled assets, skip
- If the Dockerfile or docker-compose handles the build: SKIP: 2 handled by Docker
- ONLY build on host if no Docker build step AND no compiled assets exist
- Output: DONE: 2 <short result> OR SKIP: 2 <reason>

After both: SETUP_OK

RULES:
- Do NOT run docker compose — handled separately
- Do NOT run migrations — handled separately
- For dockerized projects, PREFER skipping host-side deps/builds — Docker handles them
- Output STATUS: <what you're doing> BEFORE each action (e.g. STATUS: reading Dockerfile)
- Output DONE: or SKIP: IMMEDIATELY after each step
- Be fast. Skip what already exists.
"""
    else:
        prompt = f"""Set up "{project.name}" for development. Tech: {project.tech_stack or 'unknown'}.

YOU MUST DO EXACTLY 2 STEPS:

STEP 1 — INSTALL DEPENDENCIES:
- Check vendor/, node_modules/, requirements — if they exist and look complete, skip
- If auth.json exists in workspace root, copy it to ~/.composer/auth.json
- Output: DONE: 1 <short result> OR SKIP: 1 <reason>

STEP 2 — BUILD FRONTEND:
- If public/build/ already has compiled assets, skip
- Otherwise: npm run build (or equivalent for the stack)
- Output: DONE: 2 <short result> OR SKIP: 2 <reason>

After both: SETUP_OK

RULES:
- Do NOT run docker compose — handled separately
- Do NOT run migrations — handled separately
- Output STATUS: <what you're doing> BEFORE each action (e.g. STATUS: running composer install)
- Output DONE: or SKIP: IMMEDIATELY after each step
- Be fast. Skip what already exists.
"""

    from openclow.providers.llm.claude import _mcp_docker

    options = ClaudeAgentOptions(
        cwd=workspace,
        system_prompt="Senior DevOps engineer. 2 steps: deps + build. Skip what exists. Use docker_exec MCP tool for container commands. Be fast.",
        model="claude-sonnet-4-6",
        allowed_tools=[
            "Read", "Glob", "Grep", "Edit", "Write",
            # Docker MCP tools — use instead of Bash
            "mcp__docker__list_containers",
            "mcp__docker__container_logs",
            "mcp__docker__container_health",
            "mcp__docker__docker_exec",
            "mcp__docker__restart_container",
            "mcp__docker__compose_build",
            "mcp__docker__compose_up",
            "mcp__docker__compose_down",
            "mcp__docker__compose_ps",
        ],
        mcp_servers={
            "docker": _mcp_docker(),
        },
        permission_mode="bypassPermissions",
        max_turns=15,
    )

    full_output = ""
    current_agent_step = 1

    try:
        async for message in query(prompt=prompt, options=options):
            if not isinstance(message, AssistantMessage):
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
                    full_output += block.text + "\n"
                    for line in block.text.split("\n"):
                        line = line.strip()
                        if not line:
                            continue

                        if line.startswith("DONE:") or line.startswith("SKIP:"):
                            is_skip = line.startswith("SKIP:")
                            rest = line[5:].strip()
                            parts = rest.split(" ", 1)
                            if parts[0].isdigit():
                                agent_idx = int(parts[0])
                                detail = parts[1] if len(parts) > 1 else ""
                            else:
                                agent_idx = current_agent_step
                                detail = rest
                            cl_idx = STEP_MAP.get(agent_idx)
                            if cl_idx is not None:
                                if is_skip:
                                    await checklist.skip_step(cl_idx, detail[:50] or "skipped")
                                else:
                                    await checklist.complete_step(cl_idx, detail[:50] or "done")
                            current_agent_step = agent_idx + 1
                            next_cl = STEP_MAP.get(current_agent_step)
                            if next_cl is not None:
                                await checklist.start_step(next_cl)

                        elif line.startswith("FAIL:"):
                            rest = line[5:].strip()
                            parts = rest.split(" ", 1)
                            if parts[0].isdigit():
                                agent_idx = int(parts[0])
                                detail = parts[1] if len(parts) > 1 else "failed"
                            else:
                                agent_idx = current_agent_step
                                detail = rest
                            cl_idx = STEP_MAP.get(agent_idx)
                            if cl_idx is not None:
                                await checklist.fail_step(cl_idx, detail[:50])

                        elif line.startswith("STATUS:"):
                            detail = line[7:].strip()[:60]
                            cl_idx = STEP_MAP.get(current_agent_step)
                            if cl_idx is not None and detail:
                                await checklist.update_step(cl_idx, detail)

                elif isinstance(block, ToolUseBlock):
                    cl_idx = STEP_MAP.get(current_agent_step)
                    if cl_idx is not None:
                        cmd = ""
                        if hasattr(block, "input") and isinstance(block.input, dict):
                            cmd = str(block.input.get("command", block.input.get("file_path", "")))[:60]
                        if cmd:
                            await checklist.update_step(cl_idx, cmd)

    except Exception as e:
        log.error("bootstrap.agent_failed", error=str(e))
        cl_idx = STEP_MAP.get(current_agent_step)
        if cl_idx is not None:
            await checklist.fail_step(cl_idx, str(e)[:50])

    has_fail = any(s["status"] == "failed" for s in checklist.steps[:4])
    return not has_fail


async def _agent_fix_docker_config(
    workspace: str, compose: str, compose_project: str, project, error_output: str,
    checklist: ChecklistReporter = None, step_idx: int = 4,
) -> bool:
    """Let the agent diagnose and fix ANY docker compose failure.

    ⚠️ LEGACY FUNCTION — Use _run_master_agent() instead.
    This function is kept for backward compatibility but is no longer called
    directly in the main bootstrap flow. Docker fixes are now handled inline
    by the master agent with unified reasoning across all steps.

    Alternative: _run_master_agent(checklist, project, workspace, compose, compose_project, port)
    """
    log.warning("bootstrap.legacy_function_called", function="_agent_fix_docker_config",
                message="This function is deprecated. Use _run_master_agent() instead.")
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock
    except ImportError:
        return False

    import platform
    arch = platform.machine()  # e.g. "arm64", "x86_64"

    prompt = f"""Docker compose failed with this error:
{error_output[-2000:]}

Workspace: {workspace}
Compose file: {compose}
Host architecture: {arch}

YOU ARE A DOCKER EXPERT. Diagnose the error above and fix it. Common issues:

1. MISSING ENV VARS (e.g. "variable is not set", "invalid spec: :/path"):
   - Read docker-compose.yml, find all ${{{{VAR}}}} references
   - Read .env, find which are missing
   - Add sensible defaults to .env
   - Create directories for volume mounts if needed

2. IMAGE NOT AVAILABLE FOR ARCHITECTURE (e.g. "no matching manifest for linux/arm64"):
   - The service's image doesn't support {arch}
   - Comment out or remove that service from docker-compose.yml using a profile
   - OR add `platform: linux/amd64` to force emulation
   - OR replace with a compatible image (e.g. seleniarm/standalone-chromium for ARM)

3. BUILD FAILURES:
   - Read the Dockerfile, check for syntax errors or missing files
   - Fix the Dockerfile or add missing files

4. PORT CONFLICTS:
   - Change the host port in docker-compose.yml or .env

5. ANY OTHER ERROR:
   - Read the error carefully, diagnose the root cause, fix it

Steps:
1. Read docker-compose.yml fully
2. Read .env if it exists
3. Diagnose the specific error from the output above
4. Apply the fix (edit .env, docker-compose.yml, Dockerfile, create dirs, etc.)
5. Output FIXED: <what you did> or FAIL: <why you can't fix it>

RULES:
- You CAN modify docker-compose.yml, .env, Dockerfiles — whatever is needed
- Be surgical — only change what's broken
- Output STATUS: <what you're doing> BEFORE each action so the user sees live progress
  Example: STATUS: reading docker-compose.yml
  Example: STATUS: replacing selenium with seleniarm for ARM
  Example: STATUS: adding missing NGINX_SSL_PATH to .env
- Be fast
"""

    from openclow.providers.llm.claude import _mcp_docker

    options = ClaudeAgentOptions(
        cwd=workspace,
        system_prompt=f"Docker expert. Diagnose and fix docker compose failures. Host arch: {arch}. Use docker MCP tools (compose_up, docker_exec, etc.) instead of Bash. Be fast and surgical.",
        model="claude-sonnet-4-6",
        allowed_tools=[
            "Read", "Glob", "Grep", "Write", "Edit",
            # Docker MCP tools — use instead of Bash
            "mcp__docker__list_containers",
            "mcp__docker__container_logs",
            "mcp__docker__container_health",
            "mcp__docker__docker_exec",
            "mcp__docker__restart_container",
            "mcp__docker__compose_build",
            "mcp__docker__compose_up",
            "mcp__docker__compose_down",
            "mcp__docker__compose_ps",
        ],
        mcp_servers={
            "docker": _mcp_docker(),
        },
        permission_mode="bypassPermissions",
        max_turns=12,
    )

    try:
        async for message in query(prompt=prompt, options=options):
            if not isinstance(message, AssistantMessage):
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
                    for line in block.text.split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("STATUS:") and checklist:
                            detail = line[7:].strip()[:60]
                            if detail:
                                await checklist.update_step(step_idx, detail)
                        if "FIXED:" in line:
                            detail = line.split("FIXED:", 1)[1].strip()[:60]
                            if checklist and detail:
                                await checklist.update_step(step_idx, f"fixed: {detail}")
                            return True
                        if "FAIL:" in line:
                            detail = line.split("FAIL:", 1)[1].strip()[:60]
                            if checklist and detail:
                                await checklist.update_step(step_idx, f"cannot fix: {detail}")
                            return False
                elif isinstance(block, ToolUseBlock) and checklist:
                    cmd = ""
                    if hasattr(block, "input") and isinstance(block.input, dict):
                        cmd = str(block.input.get("command", block.input.get("file_path", "")))[:60]
                    if cmd:
                        await checklist.update_step(step_idx, cmd)
        return True  # Agent finished without explicit signal — assume it tried
    except Exception as e:
        log.error("bootstrap.agent_fix_docker_failed", error=str(e))
        return False


async def _step_agent_migrations(
    checklist: ChecklistReporter, project, workspace: str, compose_project: str,
) -> None:
    """Step 5: Agent-driven database migrations.

    The agent reads the project, identifies the framework, finds the right
    container, and runs migrations + seeders. It handles edge cases like
    working directories, waiting for DB readiness, and custom migration scripts.

    ⚠️ LEGACY FUNCTION — Use _run_master_agent() instead.
    This function is kept for backward compatibility but is no longer used
    in the main bootstrap flow. Migrations are now handled by the master agent
    as part of its unified setup process (Step 5).

    Alternative: _run_master_agent(checklist, project, workspace, compose, compose_project, port)
    """
    log.warning("bootstrap.legacy_function_called", function="_step_agent_migrations",
                message="This function is deprecated. Use _run_master_agent() instead.")
    # Get running containers info for the agent
    containers = await _get_compose_containers(compose_project, workspace, project.id)
    running = [c for c in containers if c["state"] == "running"]
    if not running:
        await checklist.skip_step(5, "no running containers")
        return

    container_info = "\n".join(
        f"  - {c['full_name']} (image: {c.get('image', '?')}, ports: {c.get('ports', '?')})"
        for c in running
    )

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
    except ImportError:
        await checklist.skip_step(5, "no agent SDK")
        return

    prompt = f"""Project "{project.name}" is running in Docker (compose project: {compose_project}).
Tech stack: {project.tech_stack or 'unknown'}.
Workspace: {workspace}

Running containers:
{container_info}

YOUR TASK: Run database migrations and seeders for this project.

STEPS:
1. Read the project to understand the framework:
   - Check composer.json, package.json, requirements.txt, Makefile, Gemfile etc.
   - Check for multi-domain/tenancy packages (gecche/laravel-multidomain, stancl/tenancy, etc.)
   - Check for .env.* files (e.g. .env.abc.test) — these indicate multi-domain setup
2. Identify which container has the app code (PHP/Python/Node — NOT mysql/redis/postgres)
3. Find the correct working directory:
   - docker inspect <container> --format '{{{{.Config.WorkingDir}}}}'
   - If the entry file (artisan, manage.py) isn't there, use docker_exec MCP tool to search: find / -name "artisan" -maxdepth 4 2>/dev/null
4. Run migrations using docker_exec MCP tool (specify the working directory):
   - Read the project to determine the exact command — don't guess
   - For multi-domain apps: include --domain=<domain> for each domain
   - For tenancy apps: run both central and tenant migrations
5. Run seeders if they exist (same domain flags apply)
6. If DB isn't ready, wait a few seconds and retry (max 3 attempts)

OUTPUT FORMAT:
- When done: DONE: <what you did>
- If no migrations needed: SKIP: <reason>
- If failed after retries: FAIL: <error>

RULES:
- ALWAYS specify the working directory when using docker_exec MCP tool
- READ the project first to understand what commands to run — don't hardcode
- Do NOT modify code — only run migration/seed commands
- Do NOT restart containers
- Output STATUS: <what you're doing> BEFORE each action (e.g. STATUS: checking composer.json for framework)
- Be fast — skip unnecessary checks
"""

    from openclow.providers.llm.claude import _mcp_docker

    options = ClaudeAgentOptions(
        cwd=workspace,
        system_prompt="Database migration specialist. Run migrations via docker_exec MCP tool. Always specify the working directory. Be fast.",
        model="claude-sonnet-4-6",
        allowed_tools=[
            "Read", "Glob", "Grep",
            # Docker MCP tools — use instead of Bash
            "mcp__docker__list_containers",
            "mcp__docker__container_logs",
            "mcp__docker__container_health",
            "mcp__docker__docker_exec",
            "mcp__docker__restart_container",
            "mcp__docker__compose_build",
            "mcp__docker__compose_up",
            "mcp__docker__compose_down",
            "mcp__docker__compose_ps",
        ],
        mcp_servers={
            "docker": _mcp_docker(),
        },
        permission_mode="bypassPermissions",
        max_turns=10,
    )

    try:
        async for message in query(prompt=prompt, options=options):
            if not isinstance(message, AssistantMessage):
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
                    for line in block.text.split("\n"):
                        line = line.strip()
                        if line.startswith("DONE:"):
                            detail = line[5:].strip()[:50] or "migrations complete"
                            await checklist.complete_step(5, detail)
                            return
                        elif line.startswith("SKIP:"):
                            detail = line[5:].strip()[:50] or "not needed"
                            await checklist.skip_step(5, detail)
                            return
                        elif line.startswith("FAIL:"):
                            detail = line[5:].strip()[:50] or "failed"
                            await checklist.fail_step(5, detail)
                            return
                        elif line.startswith("STATUS:"):
                            detail = line[7:].strip()[:60]
                            if detail:
                                await checklist.update_step(5, detail)
                elif isinstance(block, ToolUseBlock):
                    cmd = ""
                    if hasattr(block, "input") and isinstance(block.input, dict):
                        cmd = str(block.input.get("command", ""))[:60]
                    if cmd:
                        await checklist.update_step(5, cmd)

        # Agent finished without explicit DONE/SKIP/FAIL
        await checklist.complete_step(5, "agent finished")
    except Exception as e:
        log.error("bootstrap.agent_migration_failed", error=str(e))
        await checklist.fail_step(5, str(e)[:50])


async def _run_master_agent(
    checklist: ChecklistReporter, project, workspace: str,
    compose: str, compose_project: str, port: int,
    prompt_override: str | None = None,
    start_step: int = 2,
    max_step: int = 6,
    complete_keyword: str = "BOOTSTRAP_COMPLETE",
    failed_keyword: str = "BOOTSTRAP_FAILED",
) -> bool:
    """Agentic master agent with ChecklistReporter streaming.

    Reusable for bootstrap (steps 2-6) and repair (steps 0-4).
    Pass prompt_override + start_step + max_step to customize.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
    except ImportError:
        log.warning("bootstrap.master_agent_no_sdk")
        await checklist.skip_step(2, "agent SDK unavailable")
        await checklist.skip_step(3, "agent SDK unavailable")
        await checklist.skip_step(4, "agent SDK unavailable")
        await checklist.skip_step(5, "agent SDK unavailable")
        await checklist.skip_step(6, "agent SDK unavailable")
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
            f"Senior DevOps engineer setting up {project.name}. "
            f"Host: {arch}. Be fast, be decisive, fix errors yourself."
        ),
        model="claude-sonnet-4-6",
        allowed_tools=[
            # No Bash — forces agent to use MCP Docker tools which handle errors
            # gracefully instead of crashing the SDK on non-zero exit codes.
            "Read", "Write", "Edit", "Glob", "Grep",
            # Docker MCP — the agent's ONLY interface for containers & commands
            "mcp__docker__compose_build",
            "mcp__docker__compose_up",
            "mcp__docker__compose_down",
            "mcp__docker__compose_ps",
            "mcp__docker__list_containers",
            "mcp__docker__container_logs",
            "mcp__docker__container_health",
            "mcp__docker__docker_exec",
            "mcp__docker__restart_container",
            # Tunnel MCP — agent manages tunnels directly
            "mcp__docker__tunnel_start",
            "mcp__docker__tunnel_stop",
            "mcp__docker__tunnel_get_url",
            "mcp__docker__tunnel_list",
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
    
    try:
        async for message in query(prompt=prompt, options=options):
            if not isinstance(message, AssistantMessage):
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
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
                            # Track Docker fix attempts
                            if current_step == 4:
                                docker_fix_attempts += 1
                                if docker_fix_attempts > max_docker_fixes:
                                    log.warning("bootstrap.docker_fixes_exhausted", 
                                                project=project.name, 
                                                attempts=docker_fix_attempts)
                        
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
                    # Show tool usage in checklist for transparency
                    if current_step <= max_step:
                        cmd = ""
                        if hasattr(block, "input") and isinstance(block.input, dict):
                            cmd = str(block.input.get("command",
                                       block.input.get("file_path",
                                       block.name)))[:60]
                        if cmd:
                            await checklist.update_step(current_step, cmd)
    
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error("bootstrap.master_agent_failed", error=str(e))
        return False
    
    return success


async def _step_health_check(
    status: StatusReporter, workspace: str, compose: str, compose_project: str,
) -> bool:
    """Step 4: Check container health with agentic repair loop.

    Returns True if all containers healthy.
    """
    from openclow.agents.doctor import repair_container, _is_container_healthy

    await status.section("Container Health Check")
    await status.add("🔄", "Waiting for containers to initialize...")

    await asyncio.sleep(12)

    # Get container statuses
    rc, ps_output = await _run(
        "docker", "compose", "-p", compose_project, "ps", "--format", "json",
        cwd=workspace,
    )

    if rc != 0 or not ps_output.strip():
        await status.add("❌", "No containers found after compose up")
        return False

    import json
    containers = []
    for line in ps_output.strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            containers.append({
                "name": data.get("Name", ""),
                "state": data.get("State", "unknown"),
                "health": data.get("Health", ""),
                "service": data.get("Service", ""),
            })
        except (json.JSONDecodeError, KeyError):
            continue

    if not containers:
        await status.add("❌", "Could not parse container statuses")
        return False

    # Show container list
    for c in containers:
        icon = "✅" if c["state"] == "running" else "❌"
        health_str = f" ({c['health']})" if c.get("health") else ""
        short = c["name"].split("-")[-1] if "-" in c["name"] else c["name"]
        await status.add(icon, f"{short}: {c['state']}{health_str}")

    # Find unhealthy ones
    unhealthy = [
        c for c in containers
        if c["state"] != "running" or c.get("health", "").lower() == "unhealthy"
    ]

    if not unhealthy:
        await status.add("✅", f"All {len(containers)} containers healthy!")
        return True

    # ── Repair unhealthy containers ──
    await status.section(f"Repairing {len(unhealthy)} Container(s)")

    all_fixed = True
    for c in unhealthy:
        container_name = c["name"]
        service = c.get("service", "")

        async def on_progress(icon, msg, _name=container_name):
            short = _name.split("-")[-1] if "-" in _name else _name
            await status.add(icon, f"[{short}] {msg}")

        repair = await repair_container(
            container=container_name,
            workspace=workspace,
            compose_file=compose,
            compose_project=compose_project,
            service_name=service,
            max_attempts=3,
            on_progress=on_progress,
        )

        if repair.fixed:
            short = container_name.split("-")[-1]
            await status.add("✅", f"{short} repaired: {repair.final_status[:60]}")
        else:
            all_fixed = False
            short = container_name.split("-")[-1]
            await status.add("❌", f"{short}: {repair.suggestion[:80]}")

    return all_fixed


async def _step_app_health(status: StatusReporter, port: int) -> bool:
    """Step 5: Check if the app is responding on its port."""
    await status.add("🔄", f"Checking app at port {port}...")

    # Try multiple times — app might be slow to start
    for attempt in range(3):
        rc, output = await _run_shell(
            f'curl -sf http://localhost:{port}/health 2>&1 || '
            f'curl -sf http://localhost:{port}/ 2>&1 || '
            f'echo NOT_RESPONDING',
            timeout=10,
        )
        if "NOT_RESPONDING" not in output:
            await status.add("✅", f"App responding on port {port}", replace_last=True)
            return True
        if attempt < 2:
            await status.add("⏳", f"App not responding yet, retrying in 10s... ({attempt+1}/3)", replace_last=True)
            await asyncio.sleep(10)

    log.warning("bootstrap.app_not_responding", port=port,
                suggestion="Check container logs: docker compose logs <service>")
    await status.add("⚠️", f"App not responding on port {port} (may still be starting)", replace_last=True)
    return False


async def _step_tunnel(status: StatusReporter, port: int, project_name: str) -> str:
    """Step 6: Start cloudflared tunnel (persisted via tunnel_service)."""
    from openclow.services.tunnel_service import start_tunnel

    await status.add("🔄", "Creating public URL...")

    try:
        url = await start_tunnel(project_name, f"http://localhost:{port}")
        if url:
            await status.add("✅", f"Public URL: {url}", replace_last=True)
            return url
        else:
            await status.add("⚠️", "Tunnel failed to start (local access only)", replace_last=True)
            return ""
    except Exception as e:
        await status.add("⚠️", f"Tunnel failed: {str(e)[:60]} (local access only)", replace_last=True)
        return ""


# ---------------------------------------------------------------------------
# Playwright verification
# ---------------------------------------------------------------------------

async def _step_verify_app(
    status: StatusReporter, tunnel_url: str, port: int, project_name: str, workspace: str,
) -> tuple[bool, str]:
    """Step 7: Playwright verification — visit app, check it works.

    Uses Claude Agent SDK + Playwright MCP to:
    1. Navigate to the app URL
    2. Wait for page load
    3. Take a screenshot
    4. Check for errors (500s, blank pages, JS errors)

    Returns (ok: bool, detail: str).
    """
    await status.section("App Verification")
    await status.add("🔄", "Launching Playwright to verify app...")

    url = tunnel_url or f"http://localhost:{port}"

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions

        options = ClaudeAgentOptions(
            cwd=workspace,
            system_prompt=(
                "You are a QA engineer verifying a newly deployed application. "
                "Your job is to open the app in a browser, check it loads correctly, "
                "and report what you see. Be concise."
            ),
            model="claude-sonnet-4-6",
            allowed_tools=[
                "mcp__playwright__browser_navigate",
                "mcp__playwright__browser_snapshot",
                "mcp__playwright__browser_take_screenshot",
            ],
            mcp_servers={
                "playwright": {
                    "command": "npx",
                    "args": ["@playwright/mcp@0.0.28", "--headless"],
                },
            },
            permission_mode="bypassPermissions",
            max_turns=4,  # Navigate + screenshot + report = 3-4 turns max
        )

        prompt = (
            f"Navigate to {url} and verify the app is working.\n\n"
            "Steps:\n"
            "1. Navigate to the URL\n"
            "2. Wait for page to fully load\n"
            "3. Take a screenshot\n"
            "4. Check for errors (500 pages, blank screens, connection refused)\n"
            "5. Describe what you see (login page, dashboard, API docs, error, etc.)\n\n"
            "At the end, output EXACTLY one of these lines:\n"
            "VERIFY_STATUS: OK\n"
            "VERIFY_STATUS: FAIL\n\n"
            "And then:\n"
            "VERIFY_DETAIL: [what you see in 1 sentence]\n"
        )

        last_text = ""
        screenshot_path = ""

        async for message in query(prompt=prompt, options=options):
            if hasattr(message, "content"):
                for block in (message.content if isinstance(message.content, list) else [message.content]):
                    if hasattr(block, "text"):
                        last_text += block.text + "\n"
                    # Capture screenshot path if the agent saved one
                    if hasattr(block, "type") and block.type == "tool_result":
                        text_content = str(block)
                        if "screenshot" in text_content.lower() and ".png" in text_content.lower():
                            import re as _re
                            match = _re.search(r"(/[\w/.-]+\.png)", text_content)
                            if match:
                                screenshot_path = match.group(1)

        # Parse result — first try text markers from the Playwright agent
        verify_ok = "VERIFY_STATUS: OK" in last_text
        detail = ""
        for line in last_text.split("\n"):
            if line.strip().startswith("VERIFY_DETAIL:"):
                detail = line.split(":", 1)[1].strip()
                break

        if not detail:
            # Extract something useful from the output
            detail = last_text.strip().split("\n")[-1][:120] if last_text.strip() else "no response"

        # Fallback: if Playwright text parsing is ambiguous, do a real HTTP check
        if not verify_ok:
            try:
                from openclow.services.docker_guard import run_docker
                compose_project = f"openclow-{project_name}"
                app_info = await _find_app_container(compose_project, workspace, None)
                if app_info:
                    container_name, internal_port = app_info
                    rc_curl, curl_out = await run_docker(
                        "docker", "exec", container_name,
                        "curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}",
                        f"http://localhost:{internal_port}/", "--max-time", "5",
                        actor="verify_fallback", timeout=10,
                    )
                    http_code = curl_out.strip()
                    if http_code.startswith("2") or http_code.startswith("3"):
                        verify_ok = True
                        detail = detail or f"HTTP {http_code} (verified via curl fallback)"
                        log.info("bootstrap.verify_curl_fallback_ok", http_code=http_code)
            except Exception as curl_err:
                log.warning("bootstrap.verify_curl_fallback_failed", error=str(curl_err))

        if verify_ok:
            await status.add("✅", f"App verified: {detail[:80]}", replace_last=True)
        else:
            await status.add("❌", f"App issues: {detail[:80]}", replace_last=True)

        log.info("bootstrap.playwright_verify",
                 project=project_name, ok=verify_ok, detail=detail[:200])

        return verify_ok, detail

    except ImportError:
        await status.add("⚠️", "Claude Agent SDK not available — skipping Playwright verification", replace_last=True)
        return False, "sdk_unavailable"
    except Exception as e:
        await status.add("⚠️", f"Playwright verification failed: {str(e)[:60]}", replace_last=True)
        log.warning("bootstrap.playwright_failed", error=str(e))
        return False, f"error: {str(e)[:100]}"


# ---------------------------------------------------------------------------
# Main bootstrap task
# ---------------------------------------------------------------------------

async def bootstrap_project(ctx: dict, project_id: int, chat_id: str, message_id: str, chat_provider_type: str = "telegram"):
    """Agentic project bootstrap with live checklist progress.

    Uses ChecklistReporter for UX and Claude Agent for smart setup.
    The LLM reads the project, plans steps, executes them, fixes errors.
    """
    chat = await factory.get_chat_by_type(chat_provider_type)

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()

    if not project:
        await chat.send_error(chat_id, message_id, "Project not found")
        return

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

    workspace = os.path.join(settings.workspace_base_path, "_cache", project.name)
    compose = project.docker_compose_file or "docker-compose.yml"
    compose_project = f"openclow-{project.name}"

    # Use allocated port for this project (deterministic, unique per project)
    from openclow.services.port_allocator import get_app_port
    port = get_app_port(project_id)

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
        for attempt in range(1, max_agent_attempts + 1):
            try:
                master_success = await _run_master_agent(
                    checklist, project, workspace, compose, compose_project, port,
                )
                break  # Success or graceful failure — don't retry
            except asyncio.CancelledError:
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
                    await _bail(f"Agent error after {attempt} attempts: {str(e)[:150]}")
                    return
        
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
                        system_prompt="DevOps engineer. Rebuild frontend assets and clear caches after tunnel URL change. Use Docker MCP tools only.",
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
        from openclow.providers.actions import open_app_btn
        rows.append(ActionRow([open_app_btn(project.id)]))
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


