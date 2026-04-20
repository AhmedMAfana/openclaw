"""Health check task — agentic health monitor with auto-repair.

NOT a status reporter. This is a self-healing loop:
1. Run health checks (containers, HTTP, databases)
2. If anything is unhealthy → auto-trigger repair via Doctor agent
3. Doctor reads logs → Claude diagnoses → fixes → retries
4. Every single step reported to Telegram in real-time
5. If repair fails → clear explanation of what's wrong + what user needs to do
"""
import asyncio
import os
import subprocess

from openclow.services.health_service import (
    HealthReport, run_full_health_check, find_project_containers,
)
from openclow.services.tunnel_service import stop_tunnel as _stop_tunnel
from openclow.utils.docker_path import get_docker_env
from openclow.utils.logging import get_logger

log = get_logger()

STATUS_ICONS = {"pass": "✅", "fail": "❌", "warn": "⚠️", "skip": "⏭️"}


def format_health_report(report: HealthReport) -> str:
    """Compact health report — only show problems, collapse healthy stuff."""
    name = report.project_name

    if not report.is_running:
        lines = [f"❌ *{name}* — No containers running"]
        failed = [c for c in report.checks if c.status == "fail"]
        for c in failed:
            lines.append(f"  • {c.name}: {c.detail}")
        return "\n".join(lines)

    # Count healthy vs unhealthy
    healthy_count = sum(1 for c in report.containers if c.state == "running")
    total = len(report.containers)
    unhealthy = [c for c in report.containers if c.state != "running"]
    failed_checks = [c for c in report.checks if c.status == "fail"]

    if not unhealthy and not failed_checks:
        # All good — super compact
        line = f"✅ *{name}* — {healthy_count}/{total} containers healthy"
        if report.tunnel_url:
            line += f"\n🔗 {report.tunnel_url}"
        return line

    # Problems found — show only problems
    lines = [f"⚠️ *{name}* — {healthy_count}/{total} containers OK"]
    for c in unhealthy:
        import re
        short = c.name.split("-")[-1] if "-" in c.name else c.name
        lines.append(f"  ❌ {short}: {c.state}")
    for c in failed_checks:
        lines.append(f"  ❌ {c.name}: {c.detail}")
    if report.tunnel_url:
        lines.append(f"🔗 {report.tunnel_url}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Message update helpers
# ---------------------------------------------------------------------------

from openclow.services.base_reporter import edit_message as _notify  # noqa: E402


async def _sync_app_url(workspace: str, tunnel_url: str, compose_project: str, project):
    """Sync container after tunnel URL change. Delegates to tunnel_service."""
    from openclow.services.tunnel_service import sync_project_tunnel
    await sync_project_tunnel(project.name, tunnel_url)


async def _notify_with_buttons(chat, chat_id, message_id, text, project_id, tunnel_url=None):
    """Update message with health-check action buttons — compact single row."""
    from openclow.providers.actions import ActionButton, project_nav_keyboard

    from openclow.providers.actions import open_app_btn
    extra = []
    extra.append(open_app_btn(project_id))
    extra.append(ActionButton("Refresh", f"health_ref:{project_id}"))
    kb = project_nav_keyboard(project_id, *extra)

    await _notify(chat, chat_id, message_id, text, keyboard=kb)


# ---------------------------------------------------------------------------
# Detect what's broken
# ---------------------------------------------------------------------------

def _find_problems(report: HealthReport) -> list[dict]:
    """Analyze a health report and return a list of problems to fix."""
    problems = []

    # Containers that are not running
    for c in report.containers:
        if c.state != "running":
            problems.append({
                "type": "container_down",
                "container": c.name,
                "state": c.state,
                "detail": f"Container {c.name} is {c.state}",
            })
        elif "unhealthy" in c.health.lower():
            problems.append({
                "type": "container_unhealthy",
                "container": c.name,
                "state": "unhealthy",
                "detail": f"Container {c.name} is unhealthy",
            })

    # Failed health checks
    for check in report.checks:
        if check.status == "fail":
            # Docker fail is covered by container checks ONLY if containers were found
            if check.name == "Docker" and report.containers:
                continue
            problems.append({
                "type": "check_failed",
                "name": check.name,
                "detail": check.detail,
            })

    return problems


# ---------------------------------------------------------------------------
# The agentic repair loop
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Lightweight repair preflight — does NOT kill containers
# ---------------------------------------------------------------------------

async def _repair_preflight(project, workspace: str, compose: str, compose_project: str) -> dict:
    """Lightweight preflight for the repair path.

    Critically different from bootstrap._preflight:
    - Does NOT run compose_down — containers may be temporarily stopped (Docker restart)
      killing them forces a full compose_up that may need a rebuild (20-30 min)
    - Does NOT stop the tunnel — it may still be reachable
    - Does NOT prune networks — unnecessary for repair
    - Does NOT run orphan stack cleanup — that's maintenance's job

    Does:
    1. Verify Docker daemon is accessible (hard requirement)
    2. Write port env vars into .env (port isolation)
    3. Verify compose file exists (agent needs it)
    4. Detect container state: stopped vs missing vs mixed
       Returns this as "container_state" so the agent knows whether
       to just compose_up (stopped) or possibly compose_build (missing).

    Raises RuntimeError if Docker is inaccessible.
    Returns dict: {"container_state": "stopped"|"missing"|"mixed", "app_port": str}
    """
    from openclow.services.port_allocator import get_port_env_vars

    _denv = get_docker_env()

    # 1. Verify Docker daemon is accessible
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=5, env=_denv,
        )
        if result.returncode != 0:
            log.error("repair_preflight.docker_unavailable", stderr=result.stderr[:200])
            raise RuntimeError("Docker daemon is not running or not accessible")
    except FileNotFoundError:
        raise RuntimeError("Docker CLI not found")

    # 2. Write port env vars directly into .env (needed for port isolation)
    port_vars = get_port_env_vars(project.id)
    env_path = os.path.join(workspace, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            env_content = f.read()
        lines = [line for line in env_content.split("\n")
                 if not any(line.startswith(f"{k}=") for k in port_vars)]
        lines.append("\n# OpenClow port isolation (auto-generated)")
        for k, v in port_vars.items():
            lines.append(f"{k}={v}")
        with open(env_path, "w") as f:
            f.write("\n".join(lines))

    # 3. Verify compose file exists
    compose_path = os.path.join(workspace, compose)
    if not os.path.exists(compose_path):
        raise RuntimeError(f"Compose file not found: {compose_path}")

    # 4. Detect container state — tells agent what action is needed
    container_state = "unknown"
    try:
        ps_result = subprocess.run(
            ["docker", "ps", "-a",
             "--filter", f"label=com.docker.compose.project={compose_project}",
             "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=5, env=_denv,
        )
        statuses = [s.strip() for s in ps_result.stdout.strip().split("\n") if s.strip()]
        if not statuses:
            container_state = "missing"  # No containers at all → compose_up (may need build)
        elif all("Exited" in s or "Created" in s for s in statuses):
            container_state = "stopped"  # All stopped → just compose_up, no build needed
        elif any("Up" in s for s in statuses) and any(
            "Exited" in s or "Created" in s for s in statuses
        ):
            container_state = "mixed"    # Some up, some stopped → partial failure
        else:
            container_state = "running_unhealthy"  # All up but health checks failing
    except Exception as e:
        log.warning("repair_preflight.state_check_failed", error=str(e))

    log.info("repair_preflight.done",
             project=project.name,
             container_state=container_state,
             app_port=port_vars.get("APP_PORT", ""),
             skipped=["compose_down", "tunnel_stop", "network_prune"])
    return {"container_state": container_state, "app_port": port_vars.get("APP_PORT", "")}


REPAIR_PROMPT = """You are fixing a running project. App already exists — focus on Docker + Verify + Tunnel.

PROJECT: {project_name}
TECH STACK: {tech_stack}
WORKSPACE: {workspace}
HOST WORKSPACE: {host_workspace}
COMPOSE FILE: {compose}
COMPOSE PROJECT: {compose_project}
HOST ARCHITECTURE: {arch}
ALLOCATED PORT: {port}

CONTAINER STATE: {container_state}
  stopped          = containers EXIST but are stopped (e.g. Docker Desktop restart) → just compose_up, NO compose_down first
  missing          = no containers exist at all → compose_up; if "image not found" → compose_build first
  mixed            = some running, some stopped → check logs on stopped ones, then compose_up
  running_unhealthy = all up but health checks failing → skip step 1, go straight to step 2
  unknown          = couldn't determine → treat as missing

DOCKER-COMPOSE CONTENTS:
```yaml
{compose_contents}
```

.ENV CONTENTS:
```
{env_contents}
```

CONTAINER STATUS:
{container_status}

PROBLEMS FOUND:
{problems_text}

YOUR MISSION — execute these steps IN ORDER:

STEP 0 — CHECK CONTAINERS:
- Call compose_ps("{compose_project}") — this is the ONLY tool for this step.
- DO NOT use docker_exec here. docker inspect is a host command — it does not exist inside containers and will hang.
- Output: STEP_DONE: 0 <X/Y containers running>

STEP 1 — START DOCKER CONTAINERS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL RULE based on CONTAINER STATE:
  • stopped → call compose_up IMMEDIATELY. Do NOT run compose_down first.
    Containers are just temporarily stopped (e.g. Docker Desktop restart).
    They still have their images — a compose_up is all that's needed.
  • missing → call compose_up; if "image not found" error → compose_build first, then compose_up
  • mixed   → call compose_up to bring up the stopped ones
  • running_unhealthy → SKIP this step (output STEP_SKIP: 1 all running), go to step 2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- compose_up("{compose}", "{compose_project}", "{workspace}")
- compose_up is NON-BLOCKING. Possible responses:
  * "SUCCESS" → containers started immediately, poll compose_ps to confirm, then STEP_DONE: 1
  * "STARTED" → docker is launching in background. Poll compose_ps every 5s (up to 10 polls = 50s)
    until all containers show as running. If still not up after 10 polls → DIAGNOSIS
  * "FAILED — image not found" → image is missing, you MUST build it first:
      1. Call compose_build("{compose}", "{compose_project}", "{workspace}")
      2. compose_build response:
         - "DONE" → build succeeded, call compose_up again immediately
         - "FAILED ..." → read error, fix Dockerfile/.env, retry compose_build once
         - "BUILDING" → build is running in background (takes 2-20min on ARM).
           You MUST call compose_build_status("{compose_project}") every 30s to poll.
           Keep polling until status returns "DONE" or "FAILED". Do NOT proceed to
           compose_up until build is DONE. Do NOT output STEP_DONE: 1 until containers
           are actually running. Output STATUS: <progress> while waiting.
  * "FAILED (exit code ...)" → read the error, fix root cause, retry once:
    - "port already allocated": list_containers() → compose_down → retry compose_up
    - bind-mount / file path error: Edit docker-compose.override.yml to remove bad mounts → retry
    - Maximum 1 retry after a fix. If still failing: output REPAIR_FAILED with the error
- After containers are confirmed running via compose_ps:
- Output: STEP_DONE: 1 <X/Y containers running>

STEP 2 — FIX ISSUES:
- container_logs for any unhealthy/crashed containers
- docker_exec to investigate (pwd, ls, cat errors)
- Fix root cause, restart container
- If all healthy: STEP_SKIP: 2 all healthy
- Output: STEP_DONE: 2 <what was fixed>

STEP 3 — VERIFY APP:
- Find the app container (usually laravel.test, app, web, etc.)
- docker_exec("<app_container>", "curl -sf http://localhost:{port}/ -o /dev/null -w %{{http_code}}")
- If fails: read logs, fix, retry
- Output: STEP_DONE: 3 <HTTP status>

STEP 4 — VERIFY APP IS SERVING:
- Find the app container (laravel.test, app, web — NOT mysql/redis)
- docker_exec("<app_container>", "curl -sf http://localhost:{port}/ -o /dev/null -w %{{http_code}}")
- Expected: 200, 301, or 302 — anything else means the app isn't ready
- If fails: read container_logs, diagnose, fix (wrong port? app not started? .env missing?)
- Max 2 fix attempts, then output STEP_FAIL: 4 <reason>
- If passes: output STEP_DONE: 4 <HTTP status> → REPAIR_COMPLETE
- The tunnel will be established automatically by the system after you finish — do NOT call tunnel_start

RULES:
- Output STATUS: <message> BEFORE every action
- Output DIAGNOSIS: <analysis> when something fails
- Output ACTION: <what you're fixing> when fixing
- Output REPAIR_COMPLETE when all steps done
- NEVER modify docker-compose.yml image names or build contexts

CRITICAL — TOOL USAGE:
- No Bash. Use ONLY Docker MCP tools for container operations.
- docker_exec(container_name, "command") for container commands
- Read/Edit/Glob for host workspace files
- Tunnels run on HOST, NOT in containers — use tunnel_start/tunnel_get_url

CRITICAL — DIAGNOSE BEFORE GIVING UP:
When a tool call fails, your first move is ALWAYS to check actual state, not to report failure.

  Tool returns "connection closed" or MCP error:
    → Call compose_ps("{compose_project}") RIGHT NOW.
    → Docker may have succeeded before the connection dropped.
    → If containers are running: output STEP_DONE and continue — the operation worked.
    → If not running: retry compose_up once. Still failing? Read container_logs for clues.

  Tool returns "TIMEOUT":
    → Call compose_ps — the operation may have completed before timing out.
    → If running: continue. If not: check container_logs for the real error.

  Tool returns "BLOCKED":
    → Read the BLOCKED reason carefully.
    → "Could not parse" = try the same operation a different way (use compose_ps, container_logs).
    → Security block = find an alternative approach that doesn't trigger the block.
    → Never treat BLOCKED as "impossible" without understanding WHY it was blocked.

  compose_up fails repeatedly:
    → Read the actual compose log: Read("/tmp/compose_up_{compose_project}.log")
    → Read container_logs for the specific container that failed
    → Check .env values, port conflicts, image availability
    → Fix the root cause, THEN retry

  compose_build returns "BUILDING":
    → Poll compose_build_status("{compose_project}") every 30s. Keep going until DONE or FAILED.
    → Do NOT attempt compose_up until build is confirmed DONE.

REPAIR_FAILED is a last resort. Only output it when:
  1. You have tried at least 2 different approaches
  2. You have read the actual error output (container logs, compose logs)
  3. You can explain SPECIFICALLY why each approach failed
  A tool call failing once is not a reason to give up — it's a reason to investigate.

- Do NOT: create alternative compose files, try different images, or experiment with configs.
  Stick to the project's compose file. Fix the environment, not the compose definition.

CRITICAL — PRIVATE PACKAGE AUTH:
- If auth.json exists in {workspace}: it contains Composer tokens for private packages (Nova, Spark, Packagist, etc.)
- BEFORE any composer install or Docker build: copy auth.json to ~/.composer/auth.json
- If Docker build fails with HTTP 401 or 403: apply auth.json and rebuild with compose_up(build=True)
- To apply inside a container: docker_exec("<container>", "cp {workspace}/auth.json ~/.composer/auth.json")
"""


async def _run_repair_loop(
    project,
    report: HealthReport,
    problems: list[dict],
    chat,
    chat_id: str,
    message_id: str,
    status_lines: list[str],
    card=None,
    existing_checklist=None,
):
    """Agentic repair — same power as bootstrap's _run_master_agent with ChecklistReporter."""
    from openclow.settings import settings
    from openclow.services.checklist_reporter import ChecklistReporter
    from openclow.worker.tasks.bootstrap import _run_master_agent
    from openclow.services.port_allocator import get_app_port
    import platform

    workspace = os.path.join(settings.workspace_base_path, "_cache", project.name)
    compose = project.docker_compose_file or "docker-compose.yml"
    compose_project = f"openclow-{project.name}"

    if not os.path.exists(workspace):
        await _notify(chat, chat_id, message_id, "⚠️ Workspace not found — run bootstrap first")
        return report

    # Lightweight preflight — verify Docker + port vars WITHOUT killing containers.
    # (bootstrap._preflight runs compose_down which kills containers that may just be
    # temporarily stopped after a Docker Desktop restart, forcing a needless full rebuild.)
    try:
        repair_ctx = await _repair_preflight(project, workspace, compose, compose_project)
    except RuntimeError as e:
        await _notify(chat, chat_id, message_id, f"❌ Docker not accessible: {e}")
        return report

    # Reuse the existing checklist if provided (smooth in-place transition from
    # "Opening" card to "Fixing" card without creating a new message).
    if existing_checklist is not None:
        checklist = existing_checklist
        await existing_checklist.stop()  # stop old heartbeat
        checklist.title = f"Fixing {project.name}"
        checklist.steps = []
        checklist.set_steps([
            "Check containers",
            "Start Docker",
            "Fix issues",
            "Verify app",
            "Create public URL",
        ])
        await checklist._force_render()
        await checklist.start()
    else:
        checklist = ChecklistReporter(
            chat, chat_id, message_id,
            title=f"Fixing {project.name}",
            subtitle=project.tech_stack or "",
        )
        checklist.set_steps([
            "Check containers",
            "Start Docker",
            "Fix issues",
            "Verify app",
            "Create public URL",
        ])
        await checklist._force_render()
        await checklist.start()

    # Read compose + env for rich context (same as bootstrap)
    arch = platform.machine()
    port = get_app_port(project.id)

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

    container_status = "\n".join(
        f"- {c.name}: {c.state} {c.health}" for c in report.containers
    ) or "No containers found"
    problems_text = "\n".join(f"- {p['detail']}" for p in problems)
    container_state = repair_ctx.get("container_state", "unknown")

    from openclow.services.docker_guard import _detect_host_workspace_path
    from openclow.settings import settings as _settings
    host_path = await _detect_host_workspace_path()
    host_workspace = workspace
    if host_path and workspace.startswith(_settings.workspace_base_path):
        host_workspace = workspace.replace(_settings.workspace_base_path, host_path, 1)

    prompt = REPAIR_PROMPT.format(
        project_name=project.name,
        tech_stack=project.tech_stack or "Unknown",
        workspace=workspace,
        host_workspace=host_workspace,
        compose=compose,
        compose_project=compose_project,
        arch=arch,
        port=port,
        compose_contents=compose_contents,
        env_contents=env_contents,
        container_status=container_status,
        problems_text=problems_text,
        container_state=container_state,
    )

    # Agent handles steps 0-3 (containers + health) only.
    # Step 4 (tunnel) is done deterministically below — same logic as the happy path.
    # Circuit breaker — if this project has failed repair 3+ consecutive times,
    # warn the user that manual intervention is likely needed. Still attempt repair
    # (don't block it) but surface the warning so they know what to expect.
    from openclow.services.config_service import get_config as _gc, set_config as _sc
    _fail_cfg = await _gc("repair_fails", project.name) or {}
    _fail_count = _fail_cfg.get("count", 0)
    if _fail_count >= 3:
        log.warning("repair.circuit_breaker_warning", project=project.name, consecutive_fails=_fail_count)
        await checklist.update_step(0, f"⚠️ {_fail_count} consecutive failures — manual check may be needed")

    await checklist.start_step(0)
    max_attempts = 2
    success = False
    for attempt in range(1, max_attempts + 1):
        try:
            success = await _run_master_agent(
                checklist, project, workspace, compose, compose_project, port,
                prompt_override=prompt,
                start_step=0,
                max_step=3,  # agent stops after "Verify app" — tunnel handled below
                complete_keyword="REPAIR_COMPLETE",
                failed_keyword="REPAIR_FAILED",
                idle_timeout=600,
            )
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("repair.agent_failed", attempt=attempt, error=str(e)[:200])
            if attempt < max_attempts:
                resume = 0
                for i in range(4):
                    if checklist.steps[i]["status"] in ("pending", "running"):
                        resume = i
                        break
                await checklist.update_step(resume, f"retrying (attempt {attempt + 1})...")

    # Step 4 — tunnel setup via deterministic code.
    # ONLY run if the agent actually fixed the containers (success=True).
    # If the agent failed, there's nothing to tunnel to — skip and show a retry button.
    from openclow.services.tunnel_service import refresh_tunnel
    from openclow.worker.tasks.bootstrap import _get_tunnel_target
    from openclow.services.port_allocator import get_app_port as _get_app_port

    await checklist.start_step(4)
    tunnel_url: str | None = None

    if not success:
        # Agent failed — no containers running, no point creating a tunnel
        await checklist.fail_step(4, "Skipped — app not running")
    else:
        try:
            # After a repair, containers may have new IPs — always refresh the tunnel.
            # Reusing the old cloudflared process (even if alive) would point at the
            # old IP → "origin has been unregistered from Argo Tunnel" in browser.
            # refresh_tunnel = stop old + start fresh with updated container IP.
            # start_tunnel → sync_project_tunnel handles .env + asset rebuild automatically.
            await checklist.update_step(4, "Starting tunnel...")
            target = await _get_tunnel_target(compose_project, workspace, project.id)
            if not target:
                target = f"http://localhost:{_get_app_port(project.id)}"
            tunnel_url = await asyncio.wait_for(
                refresh_tunnel(project.name, target), timeout=60,
            )

            if tunnel_url:
                await checklist.complete_step(4, tunnel_url)
            else:
                await checklist.fail_step(4, "Rate limited — retry in 1-2 min")
        except Exception as e:
            log.warning("repair.tunnel_failed", error=str(e))
            await checklist.fail_step(4, str(e)[:60])

    # Re-check health for accurate final report
    report = await asyncio.wait_for(
        run_full_health_check(project, with_tunnel=True),
        timeout=30,
    )
    tunnel_url = tunnel_url or report.tunnel_url

    await checklist.finalize(footer=tunnel_url or "", success=success)

    # Update circuit breaker counter: reset on success, increment on failure.
    if success and tunnel_url:
        await _sc("repair_fails", project.name, {"count": 0})
    else:
        await _sc("repair_fails", project.name, {"count": _fail_count + 1})

    # Telegram/Slack: send message with action buttons (_force_render is a no-op for web)
    from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
    if tunnel_url:
        kb = ActionKeyboard(rows=[
            ActionRow([ActionButton("🌐 Open in Browser", "open_browser", url=tunnel_url, style="primary")]),
            ActionRow([ActionButton("🔄 Refresh", f"health_ref:{project.id}"), ActionButton("◀️ Menu", "menu:main")]),
        ])
    else:
        kb = ActionKeyboard(rows=[
            ActionRow([ActionButton("🔄 Retry", f"health_ref:{project.id}", style="primary")]),
            ActionRow([ActionButton("◀️ Menu", "menu:main")]),
        ])
    await checklist._force_render(keyboard=kb)

    return report


# ---------------------------------------------------------------------------
# Main worker tasks
# ---------------------------------------------------------------------------

async def check_project_health(ctx: dict, project_id: int, chat_id: str, message_id: str, chat_provider_type: str = "telegram"):
    """Worker task: health check → agentic repair with ChecklistReporter (same as bootstrap)."""
    from openclow.models import Project, async_session
    from openclow.providers import factory
    from sqlalchemy import select

    chat = await factory.get_chat_by_type(chat_provider_type)

    try:
        async with async_session() as session:
            result = await session.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if project:
                session.expunge(project)

        if not project:
            await chat.edit_message(chat_id, message_id, "Project not found.")
            return

        if (getattr(project, "mode", "docker") or "docker") == "host":
            return await _check_project_health_host(project, chat, chat_id, message_id)

        from openclow.settings import settings
        from openclow.services.checklist_reporter import ChecklistReporter
        from openclow.services.tunnel_service import get_tunnel_url, check_tunnel_health, ensure_tunnel
        from openclow.worker.tasks.bootstrap import _get_tunnel_target
        from openclow.services.port_allocator import get_app_port
        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow, project_nav_keyboard, open_app_btn
        import aiohttp

        compose_project = f"openclow-{project.name}"
        workspace = os.path.join(settings.workspace_base_path, "_cache", project.name)

        # ── ChecklistReporter for smooth UI (same as bootstrap) ──
        checklist = ChecklistReporter(
            chat, chat_id, message_id,
            title=f"Opening {project.name}",
        )
        checklist._heartbeat_interval = 1.0
        checklist._rate_limit = 0.8
        checklist.set_steps(["Check containers", "Verify app", "Connect tunnel"])
        await checklist.start_step(0)  # Start immediately — progress bar shows activity
        await checklist._force_render()
        await checklist.start()

        # Step 0: Check containers
        report = await asyncio.wait_for(
            run_full_health_check(project, with_tunnel=True),
            timeout=30,
        )
        problems = _find_problems(report)

        # Happy path requires: all containers running AND HTTP responding.
        # If any container is down, unhealthy, or the HTTP check fails — drop into
        # the agentic repair loop so the user sees the detailed log of what's being
        # fixed instead of a too-fast green checkmark with "may need time".
        running_count = sum(1 for c in report.containers if c.state == "running")
        total_count = len(report.containers)
        all_containers_up = report.containers and running_count == total_count
        http_ok = any(c.status == "pass" for c in report.checks if c.name == "HTTP")
        critical_problems = [p for p in problems if p["type"] in
                              ("container_down", "container_unhealthy")
                              or (p["type"] == "check_failed" and p.get("name") == "HTTP")]

        if report.is_running and all_containers_up and http_ok and not critical_problems:
            await checklist.complete_step(0, f"{running_count}/{total_count} running")

            # Step 1: Verify app responds — retry a few times if the container
            # is still warming up, then fail honestly if it never responds.
            await checklist.start_step(1)
            app_ok = any(c.status == "pass" for c in report.checks if c.name == "HTTP")
            if not app_ok:
                # Re-probe up to 3 times (total ~15s) before giving up
                for attempt in range(3):
                    await checklist.update_step(1, f"probing app ({attempt + 1}/3)...")
                    await asyncio.sleep(5)
                    try:
                        retry_report = await asyncio.wait_for(
                            run_full_health_check(project, with_tunnel=False),
                            timeout=10,
                        )
                        if any(c.status == "pass" for c in retry_report.checks if c.name == "HTTP"):
                            app_ok = True
                            break
                    except Exception as _probe_err:
                        log.warning("health.probe_failed", project=project.name,
                                    attempt=attempt + 1, error=str(_probe_err))
            if app_ok:
                await checklist.complete_step(1, "HTTP OK")
            else:
                # App not responding — hand off to the agentic repair loop so
                # a senior-engineer LLM can diagnose and fix, with the detailed
                # live log visible to the user.
                await checklist.update_step(1, "app down — dispatching repair agent")
                new_problems = [{
                    "type": "check_failed",
                    "name": "HTTP",
                    "detail": "app not responding after 3 probes — containers up but HTTP check fails",
                }]
                report = await _run_repair_loop(
                    project, report, new_problems, chat, chat_id, message_id, [],
                    existing_checklist=checklist,
                )
                log.info("health.check_done", project=project.name,
                         running=report.is_running, problems=len(new_problems),
                         tunnel=report.tunnel_url is not None)
                return

            # Step 2: Tunnel
            await checklist.start_step(2)
            tunnel_url = await get_tunnel_url(project.name)

            # Verify tunnel actually works (not stale/dead)
            if tunnel_url:
                try:
                    async with aiohttp.ClientSession() as session_http:
                        async with session_http.get(tunnel_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status >= 500:
                                tunnel_url = None  # Dead tunnel
                except Exception:
                    tunnel_url = None

            if not tunnel_url:
                await checklist.update_step(2, "Starting tunnel...")
                try:
                    target = await _get_tunnel_target(compose_project, workspace, project_id)
                    if not target:
                        target = f"http://localhost:{get_app_port(project_id)}"
                    tunnel_url = await asyncio.wait_for(
                        ensure_tunnel(project.name, target), timeout=30,
                    )
                    if tunnel_url:
                        await _sync_app_url(workspace, tunnel_url, compose_project, project)
                except Exception as e:
                    log.warning("health.tunnel_failed", error=str(e))
                    tunnel_url = None

            if tunnel_url:
                await checklist.complete_step(2, tunnel_url)
            else:
                await checklist.fail_step(2, "Rate limited — retry in 1-2 min")

            await checklist.stop()

            # Final buttons — no Open App btn (prevents endless loop)
            if tunnel_url:
                kb = ActionKeyboard(rows=[
                    ActionRow([ActionButton("🌐 Open in Browser", "open_browser", url=tunnel_url, style="primary")]),
                    ActionRow([
                        ActionButton("🔄 Refresh", f"health_ref:{project_id}"),
                        ActionButton("◀️ Menu", "menu:main"),
                    ]),
                ])
            else:
                kb = ActionKeyboard(rows=[
                    ActionRow([ActionButton("🔄 Retry", f"health_ref:{project_id}", style="primary")]),
                    ActionRow([ActionButton("◀️ Menu", "menu:main")]),
                ])
            await checklist._force_render(keyboard=kb)
        else:
            # Problems found — hand off existing checklist so the card updates
            # in-place instead of jumping to a brand-new card.
            report = await _run_repair_loop(
                project, report, problems, chat, chat_id, message_id, [],
                existing_checklist=checklist,
            )

        log.info("health.check_done", project=project.name, running=report.is_running,
                 problems=len(problems), tunnel=report.tunnel_url is not None)

    except asyncio.TimeoutError:
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        kb = project_nav_keyboard(project_id, ActionButton("🔄 Retry", f"health_ref:{project_id}", style="primary"))
        await _notify(chat, chat_id, message_id, f"⏱ Health check timed out", keyboard=kb)
    except asyncio.CancelledError:
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        kb = project_nav_keyboard(project_id, ActionButton("🔄 Retry", f"health_ref:{project_id}", style="primary"))
        await _notify(chat, chat_id, message_id, f"⏹ Cancelled", keyboard=kb)
        raise
    except Exception as e:
        log.error("health.check_failed", error=str(e))
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        kb = project_nav_keyboard(project_id, ActionButton("🔄 Retry", f"health_ref:{project_id}", style="primary"))
        await _notify(chat, chat_id, message_id, f"❌ {str(e)[:100]}", keyboard=kb)


async def stop_tunnel_task(ctx: dict, project_id: int, chat_id: str, message_id: str, chat_provider_type: str = "telegram"):
    """Worker task: stop a running tunnel for a project."""
    from openclow.models import Project, async_session
    from openclow.providers import factory
    from sqlalchemy import select

    chat = await factory.get_chat_by_type(chat_provider_type)

    try:
        async with async_session() as session:
            result = await session.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if project:
                session.expunge(project)

        from openclow.providers.actions import ActionButton, project_nav_keyboard, nav_keyboard

        if project:
            await _stop_tunnel(project.name)
            kb = project_nav_keyboard(project_id, ActionButton("Docker Up", f"project_up:{project_id}"))
            await _notify(chat, chat_id, message_id, f"✅ Tunnel stopped for {project.name}.", keyboard=kb)
        else:
            await _notify(chat, chat_id, message_id, "Project not found.", keyboard=nav_keyboard())
    except asyncio.CancelledError:
        from openclow.providers.actions import project_nav_keyboard
        await _notify(chat, chat_id, message_id, "⏹ Tunnel stop cancelled.",
                      keyboard=project_nav_keyboard(project_id))
        raise
    except Exception as e:
        log.error("health.stop_tunnel_failed", error=str(e))
        from openclow.providers.actions import project_nav_keyboard
        await _notify(chat, chat_id, message_id, f"❌ Failed to stop tunnel: {str(e)[:200]}",
                      keyboard=project_nav_keyboard(project_id))


# ---------------------------------------------------------------------------
# Host-mode health check (mode="host" projects)
# ---------------------------------------------------------------------------


async def _check_project_health_host(project, chat, chat_id: str, message_id: str):
    """Host-mode health check: no docker-compose, no container inspection.
    Just: process status → HTTP curl → tunnel refresh. Self-heals by calling
    host_start_app if the process isn't running."""
    from openclow.services.checklist_reporter import ChecklistReporter
    from openclow.services.host_guard import run_host
    from openclow.services.tunnel_service import get_tunnel_url, ensure_tunnel
    from openclow.providers.actions import (
        ActionButton, ActionKeyboard, ActionRow, project_nav_keyboard,
    )

    checklist = ChecklistReporter(
        chat, chat_id, message_id,
        title=f"Opening {project.name} (host mode)",
    )
    checklist.set_steps(["Check process", "Verify HTTP", "Connect tunnel"])
    await checklist._force_render()
    await checklist.start()

    try:
        await checklist.start_step(0)
        if project.project_dir:
            rc, out = await run_host(
                f"ps -eo pid,cmd | grep -F {project.name!r} | grep -v grep",
                cwd=project.project_dir, actor="health", timeout=5,
                project_name=project.name, project_id=project.id,
            )
            process_running = rc == 0 and bool(out.strip())
        else:
            process_running = False

        if not process_running and project.project_dir and project.start_command:
            await checklist.update_step(0, "app down — restarting")
            log_path = f"{project.project_dir}/.openclow-start.log"
            rc, _ = await run_host(
                f"nohup setsid sh -c {project.start_command!r} > {log_path} 2>&1 < /dev/null &",
                cwd=project.project_dir, actor="health", timeout=10,
                project_name=project.name, project_id=project.id,
            )
            import asyncio as _a
            await _a.sleep(3)
        await checklist.complete_step(
            0, "running" if process_running else "restarted",
        )

        await checklist.start_step(1)
        health_url = project.health_url or (
            f"http://localhost:{project.app_port}/" if project.app_port else ""
        )
        http_ok = False
        http_code = "000"
        if health_url:
            rc, out = await run_host(
                f"curl -sS -o /dev/null -w '%{{http_code}}' --max-time 5 {health_url}",
                cwd=project.project_dir or "/tmp", actor="health", timeout=8,
                project_name=project.name, project_id=project.id,
            )
            http_code = (out.strip() or "000")[:3]
            http_ok = rc == 0 and http_code[0] in "23"
        if http_ok:
            await checklist.complete_step(1, f"HTTP {http_code}")
        else:
            await checklist.fail_step(1, f"HTTP {http_code}")

        await checklist.start_step(2)
        tunnel_enabled = getattr(project, "tunnel_enabled", True)
        configured_url = getattr(project, "public_url", None)
        tunnel_url: str | None = None

        if configured_url and not tunnel_enabled:
            # nginx + owned domain — skip cloudflared entirely
            tunnel_url = configured_url
            await checklist.complete_step(2, f"domain: {configured_url[:48]}")
        else:
            tunnel_url = await get_tunnel_url(project.name) if http_ok else None
            if http_ok and not tunnel_url and project.app_port:
                try:
                    import asyncio as _a
                    tunnel_url = await _a.wait_for(
                        ensure_tunnel(project.name,
                                      f"http://host.docker.internal:{project.app_port}"),
                        timeout=30,
                    )
                except Exception as e:
                    log.warning("health.host_tunnel_failed", error=str(e))

            if tunnel_url:
                await checklist.complete_step(2, tunnel_url)
            elif not http_ok:
                await checklist.fail_step(2, "skipped — app down")
            else:
                await checklist.fail_step(2, "tunnel failed")

        await checklist.stop()

        if tunnel_url:
            kb = ActionKeyboard(rows=[
                ActionRow([ActionButton("🌐 Open in Browser", "open_browser",
                                        url=tunnel_url, style="primary")]),
                ActionRow([
                    ActionButton("🔄 Refresh", f"health_ref:{project.id}"),
                    ActionButton("◀️ Menu", "menu:main"),
                ]),
            ])
        else:
            kb = ActionKeyboard(rows=[
                ActionRow([ActionButton("🔄 Retry", f"health_ref:{project.id}",
                                        style="primary")]),
                ActionRow([ActionButton("◀️ Menu", "menu:main")]),
            ])
        await checklist._force_render(keyboard=kb)

    except asyncio.CancelledError:
        await checklist.stop()
        raise
    except Exception as e:
        log.error("health.host_failed", error=str(e), project=project.name)
        await checklist.stop()
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        kb = project_nav_keyboard(project.id,
                                  ActionButton("🔄 Retry", f"health_ref:{project.id}",
                                               style="primary"))
        await _notify(chat, chat_id, message_id, f"❌ {str(e)[:120]}", keyboard=kb)
