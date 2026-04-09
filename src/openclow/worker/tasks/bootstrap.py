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
    """
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
        # Log the error but continue to poll — partial success is OK
        key_error = _parse_docker_error(output)
        log.warning("bootstrap.compose_up_partial", error=key_error)
        await checklist.update_step(4, f"some errors, checking status...")

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
    """Extract clean error message from docker compose output. Generic."""
    for line in output.split("\n"):
        line = line.strip()
        # Strip Docker noise
        line = re.sub(r'time="[^"]*"\s*', '', line)
        line = re.sub(r'level=\w+\s*', '', line)
        line = re.sub(r'msg="([^"]*)"', r'\1', line)
        line = line.strip()
        if not line or "warning" in line.lower() or "obsolete" in line.lower():
            continue
        if any(kw in line.lower() for kw in ("error", "denied", "failed", "not found", "refused")):
            return line[:80]
    return "compose up failed"


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
    url_vars = {
        "APP_URL": tunnel_url,
        "ASSET_URL": tunnel_url,
        "VITE_API_BASE_URL": tunnel_url + "/",
        "VITE_APP_URL": tunnel_url,
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
    # No container restart needed — .env changes apply on next request (Laravel reads .env on boot)

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
    """Steps 2-3 + 5: Agent handles deps, build, migrations.
    Step 4 (Docker) is handled by _step_docker_up() — Python-driven.

    The agent outputs DONE: 1 (deps), DONE: 2 (build), DONE: 3 (migrations).
    """
    STEP_MAP = {1: 2, 2: 3}  # agent step → checklist index (only deps + build)

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
    except ImportError:
        await checklist.skip_step(2, "no SDK")
        await checklist.skip_step(3, "no SDK")
        docker_ok = await _step_docker_up(checklist, project, workspace, compose, compose_project)
        await checklist.skip_step(5, "no SDK")
        return docker_ok

    await checklist.start_step(2)

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
- Output DONE: or SKIP: IMMEDIATELY after each step
- Be fast. Skip what already exists.
"""

    options = ClaudeAgentOptions(
        cwd=workspace,
        system_prompt="Senior DevOps engineer. 2 steps: deps + build. Skip what exists. Be fast.",
        model="claude-sonnet-4-6",
        allowed_tools=["Bash", "Read", "Glob", "Grep", "Edit", "Write"],
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

                elif isinstance(block, ToolUseBlock):
                    cl_idx = STEP_MAP.get(current_agent_step)
                    if cl_idx is not None:
                        cmd = ""
                        if hasattr(block, "input") and isinstance(block.input, dict):
                            cmd = str(block.input.get("command", block.input.get("file_path", "")))[:40]
                        if cmd:
                            await checklist.update_step(cl_idx, cmd)

    except Exception as e:
        log.error("bootstrap.agent_failed", error=str(e))
        cl_idx = STEP_MAP.get(current_agent_step)
        if cl_idx is not None:
            await checklist.fail_step(cl_idx, str(e)[:50])

    # ── Step 4: Docker (Python-driven, after agent finishes deps+build) ──
    docker_ok = await _step_docker_up(checklist, project, workspace, compose, compose_project)

    has_fail = any(s["status"] == "failed" for s in checklist.steps)
    return docker_ok and not has_fail


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

        # Parse result
        verify_ok = "VERIFY_STATUS: OK" in last_text
        detail = ""
        for line in last_text.split("\n"):
            if line.strip().startswith("VERIFY_DETAIL:"):
                detail = line.split(":", 1)[1].strip()
                break

        if not detail:
            # Extract something useful from the output
            detail = last_text.strip().split("\n")[-1][:120] if last_text.strip() else "no response"

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

async def bootstrap_project(ctx: dict, project_id: int, chat_id: str, message_id: str):
    """Agentic project bootstrap with live checklist progress.

    Uses ChecklistReporter for UX and Claude Agent for smart setup.
    The LLM reads the project, plans steps, executes them, fixes errors.
    """
    chat = await factory.get_chat()

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

    workspace = os.path.join(settings.workspace_base_path, "_cache", project.name)
    compose = project.docker_compose_file or "docker-compose.yml"
    compose_project = f"openclow-{project.name}"

    # Use allocated port for this project (deterministic, unique per project)
    from openclow.services.port_allocator import get_app_port
    port = get_app_port(project_id)

    if not message_id or message_id == "0":
        bot = chat._get_bot()
        msg = await bot.send_message(chat_id=int(chat_id), text=f"Setting up {project.name}...")
        message_id = str(msg.message_id)

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

        # ── Write port isolation env vars ──
        from openclow.services.port_allocator import get_port_env_vars
        port_vars = get_port_env_vars(project.id)
        ports_file = os.path.join(workspace, ".env.ports")
        with open(ports_file, "w") as f:
            f.write("# Auto-generated by OpenClow — unique ports for this project\n")
            for k, v in port_vars.items():
                f.write(f"{k}={v}\n")

        # ── Step 2: Install dependencies — agent handles, validates result ──
        await checklist.start_step(2)
        await checklist.update_step(2, "checking...")
        # Let the agent decide: install or verify existing deps
        await _step_agentic_setup(checklist, project, workspace, compose, compose_project)
        # If agent didn't mark step 2, validate ourselves
        if checklist.steps[2]["status"] == "running":
            # Agent didn't output DONE/SKIP — check if deps exist now
            has_deps = (
                os.path.exists(os.path.join(workspace, "vendor", "autoload.php")) or
                os.path.exists(os.path.join(workspace, "node_modules", ".package-lock.json")) or
                os.path.exists(os.path.join(workspace, ".venv", "bin", "python"))
            )
            if has_deps:
                await checklist.complete_step(2, "verified")
            else:
                await checklist.fail_step(2, "deps not found after install")

        # ── Step 3: Build frontend — validate build output exists ──
        if checklist.steps[3]["status"] == "running" or checklist.steps[3]["status"] == "pending":
            await checklist.start_step(3)
            build_dir = os.path.join(workspace, "public", "build")
            dist_dir = os.path.join(workspace, "dist")
            out_dir = os.path.join(workspace, "out")
            has_pkg = os.path.exists(os.path.join(workspace, "package.json"))

            if not has_pkg:
                await checklist.skip_step(3, "no package.json — not a frontend project")
            elif os.path.isdir(build_dir) and len(os.listdir(build_dir)) > 1:
                await checklist.complete_step(3, f"public/build/ verified ({len(os.listdir(build_dir))} files)")
            elif os.path.isdir(dist_dir) and len(os.listdir(dist_dir)) > 0:
                await checklist.complete_step(3, f"dist/ verified ({len(os.listdir(dist_dir))} files)")
            elif os.path.isdir(out_dir) and len(os.listdir(out_dir)) > 0:
                await checklist.complete_step(3, f"out/ verified ({len(os.listdir(out_dir))} files)")
            else:
                # Agent should have built it — if not, run npm build ourselves
                await checklist.update_step(3, "building assets...")
                rc, out = await _run("npm", "run", "build", cwd=workspace, timeout=300)
                if rc == 0:
                    await checklist.complete_step(3, "built successfully")
                else:
                    err = out.strip().split("\n")[-1][:50] if out.strip() else "build failed"
                    await checklist.fail_step(3, err)

        # ── Step 4: Start Docker containers — wait until all running ──
        docker_ok = await _step_docker_up(checklist, project, workspace, compose, compose_project)
        if not docker_ok:
            # Docker failed — don't continue to tunnel
            checklist._footer = "❌ Docker failed — fix errors and retry"
            from aiogram.types import InlineKeyboardButton
            buttons = [
                [InlineKeyboardButton(text="🔄 Retry Bootstrap", callback_data=f"project_bootstrap:{project.id}")],
                [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
            ]
            await checklist.stop()
            await checklist._force_render(buttons=buttons)
            return

        # ── Step 5: Run database migrations (via docker exec — AFTER Docker is up) ──
        await checklist.start_step(5)

        # Search ALL project containers for migration tools (not just the web container)
        containers = await _get_compose_containers(compose_project, workspace, project.id)
        migration_cmds = [
            ("php", ["php", "artisan", "migrate", "--force"]),
            ("python", ["python", "manage.py", "migrate", "--noinput"]),
            ("node", ["npx", "prisma", "migrate", "deploy"]),
        ]
        migrated = False
        for c in containers:
            if c["state"] != "running":
                continue
            for lang, cmd in migration_cmds:
                rc_check, _ = await _run("docker", "exec", c["full_name"], "which", cmd[0])
                if rc_check != 0:
                    continue
                await checklist.update_step(5, f"{c['name']}: {' '.join(cmd[:3])}...")
                rc_mig, mig_out = await _run(
                    "docker", "exec", c["full_name"], *cmd,
                    timeout=120,
                )
                if rc_mig == 0:
                    await checklist.complete_step(5, f"{lang} migrations via {c['name']}")
                else:
                    err = mig_out.strip().split("\n")[-1][:50] if mig_out.strip() else "failed"
                    await checklist.fail_step(5, err)
                migrated = True
                break
            if migrated:
                break
        if not migrated:
            await checklist.skip_step(5, "no migration tool found")

        # ── Step 6: Verify app (via docker exec — generic) ──
        await checklist.start_step(6)
        app_info = await _find_app_container(compose_project, workspace, project.id)
        app_ok = False

        if app_info:
            container_name, internal_port = app_info
            # Try 3 times with 5s gaps — app may need time to start
            for attempt in range(3):
                await checklist.update_step(6, f"checking {container_name}... ({attempt+1}/3)")
                rc, curl_out = await _run(
                    "docker", "exec", container_name,
                    "curl", "-sf", f"http://localhost:{internal_port}/",
                    "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5",
                )
                http_code = curl_out.strip()
                if http_code.startswith("2") or http_code.startswith("3"):
                    app_ok = True
                    await checklist.complete_step(6, f"HTTP {http_code} from {container_name}")
                    break
                if attempt < 2:
                    await asyncio.sleep(5)

            if not app_ok:
                await checklist.fail_step(6, f"HTTP {http_code} — app not ready")
        else:
            await checklist.fail_step(6, "no web container found")

        # ── Step 7: Create public URL (ONLY if verify passed) ──
        if not app_ok:
            await checklist.fail_step(7, "skipped — app not verified")
            checklist._footer = "⚠️ App not responding — tunnel not created"
            from aiogram.types import InlineKeyboardButton
            buttons = [
                [InlineKeyboardButton(text="🔄 Retry Bootstrap", callback_data=f"project_bootstrap:{project.id}")],
                [InlineKeyboardButton(text="💚 Health Check", callback_data=f"health:{project.id}")],
                [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
            ]
            await checklist.stop()
            await checklist._force_render(buttons=buttons)
            log.warning("bootstrap.no_tunnel", project=project.name, reason="app_not_responding")
            return

        await checklist.start_step(7)
        from openclow.services.tunnel_service import start_tunnel
        tunnel_url = ""
        try:
            # Get tunnel target via container IP (not localhost — worker can't reach host ports)
            tunnel_target = await _get_tunnel_target(compose_project, workspace, project.id)
            if not tunnel_target:
                tunnel_target = f"http://localhost:{port}"  # fallback

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
                # Rebuild frontend so VITE_API_BASE_URL points to tunnel
                if os.path.exists(os.path.join(workspace, "package.json")):
                    rc_build, _ = await _run("npm", "run", "build", cwd=workspace, timeout=300)
                    if rc_build != 0:
                        log.warning("bootstrap.rebuild_failed", project=project.name)
                # Clear Laravel caches so new APP_URL takes effect
                for container in await _get_compose_containers(compose_project, workspace, project.id):
                    if container["state"] == "running":
                        rc_chk, _ = await _run("docker", "exec", container["full_name"], "which", "php")
                        if rc_chk == 0:
                            await _run("docker", "exec", container["full_name"],
                                       "php", "artisan", "config:clear", timeout=10)
                            await _run("docker", "exec", container["full_name"],
                                       "php", "artisan", "cache:clear", timeout=10)
                            break
                await checklist.complete_step(7, url[:50])
            else:
                await checklist.fail_step(7, "tunnel failed to start")
        except Exception as e:
            await checklist.fail_step(7, str(e)[:40])

        # ── Final summary with buttons ──
        checklist._footer = ""
        all_done = all(s["status"] in ("done", "skipped") for s in checklist.steps)
        any_failed = any(s["status"] == "failed" for s in checklist.steps)

        if all_done:
            checklist._footer = "🚀 Ready for tasks!"
        elif any_failed:
            checklist._footer = "⚠️ Some steps had issues — check above"
        else:
            checklist._footer = "✅ Setup complete"

        if tunnel_url:
            checklist._footer += f"\n🌐 {tunnel_url}"

        from aiogram.types import InlineKeyboardButton
        buttons = [
            [
                InlineKeyboardButton(text="🚀 New Task", callback_data="menu:task"),
                InlineKeyboardButton(text="💚 Health", callback_data=f"health:{project.id}"),
            ],
        ]
        if tunnel_url:
            buttons.insert(0, [InlineKeyboardButton(text="🌐 Open App", url=tunnel_url)])

        await checklist.stop()
        await checklist._force_render(buttons=buttons)

        log.info("bootstrap.complete", project=project.name, all_done=all_done)

    except (Exception, asyncio.CancelledError, TimeoutError) as e:
        await checklist.stop()
        error_msg = "Job timed out" if isinstance(e, (asyncio.CancelledError, TimeoutError)) else str(e)[:150]
        log.error("bootstrap.failed", project=project.name, error=error_msg)
        checklist._footer = f"❌ {error_msg}"
        try:
            from aiogram.types import InlineKeyboardButton
            await checklist._force_render(buttons=[
                [InlineKeyboardButton(text="🔄 Retry Bootstrap", callback_data=f"project_bootstrap:{project.id}")],
                [
                    InlineKeyboardButton(text="🔗 Unlink", callback_data=f"project_unlink:{project.id}"),
                    InlineKeyboardButton(text="🗑 Remove", callback_data=f"project_remove:{project.id}"),
                ],
                [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
            ])
        except Exception:
            pass
    finally:
        if lock:
            await lock.release()
        from openclow.services.audit_service import flush
        await flush()
        await chat.close()


