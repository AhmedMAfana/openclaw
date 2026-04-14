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

from openclow.services.health_service import (
    HealthReport, run_full_health_check, find_project_containers,
)
from openclow.services.tunnel_service import stop_tunnel as _stop_tunnel
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

REPAIR_PROMPT = """You are fixing a running project. App already exists — focus on Docker + Verify + Tunnel.

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

CONTAINER STATUS:
{container_status}

PROBLEMS FOUND:
{problems_text}

YOUR MISSION — execute these steps IN ORDER:

STEP 0 — CHECK CONTAINERS:
- compose_ps("{compose_project}") to see current state
- Output: STEP_DONE: 0 <X/Y containers running>

STEP 1 — START DOCKER CONTAINERS:
- compose_up("{compose}", "{compose_project}", "{workspace}")
- If it FAILS:
  * Read the error output carefully
  * DIAGNOSIS: <your analysis>
  * If "port already allocated": list_containers() → find orphans → compose_down
  * If image missing: compose_up with build=True
  * ACTION: <what you're fixing>
  * Retry compose_up
  * You get up to 3 fix attempts
- After containers start, verify ALL are running via compose_ps
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

STEP 4 — CREATE PUBLIC URL:
- tunnel_get_url("{project_name}") — check if tunnel exists
- If no tunnel: tunnel_start("{project_name}", "http://<app_container_ip>:{port}")
- Output: STEP_DONE: 4 <tunnel_url>

RULES:
- Output STATUS: <message> BEFORE every action
- Output DIAGNOSIS: <analysis> when something fails
- Output ACTION: <what you're fixing> when fixing
- Output REPAIR_COMPLETE when all steps done
- Be FAST — act decisively
- NEVER modify docker-compose.yml image names or build contexts
- Use compose_up(build=True) if image is missing

CRITICAL — TOOL USAGE:
- No Bash. Use ONLY Docker MCP tools for container operations.
- docker_exec(container_name, "command") for container commands
- Read/Edit/Glob for host workspace files
- Tunnels run on HOST, NOT in containers — use tunnel_start/tunnel_get_url

CRITICAL — SELF-HEALING:
- When ANY command fails, investigate before giving up
- Fix the root cause, retry with the fix
- NEVER give up after one failure — try at least 2 approaches
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

    # Preflight: same cleanup as bootstrap (kill orphans, free ports, verify Docker)
    from openclow.worker.tasks.bootstrap import _preflight
    try:
        await _preflight(project, workspace, compose, compose_project)
    except RuntimeError as e:
        await _notify(chat, chat_id, message_id, f"❌ Preflight failed: {e}")
        return report

    # ChecklistReporter — same beautiful UI as bootstrap
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

    prompt = REPAIR_PROMPT.format(
        project_name=project.name,
        tech_stack=project.tech_stack or "Unknown",
        workspace=workspace,
        compose=compose,
        compose_project=compose_project,
        arch=arch,
        port=port,
        compose_contents=compose_contents,
        env_contents=env_contents,
        container_status=container_status,
        problems_text=problems_text,
    )

    # Run bootstrap's master agent with repair prompt — same streaming, same power
    await checklist.start_step(0)
    max_attempts = 2
    success = False
    for attempt in range(1, max_attempts + 1):
        try:
            success = await _run_master_agent(
                checklist, project, workspace, compose, compose_project, port,
                prompt_override=prompt,
                start_step=0,
                max_step=4,
                complete_keyword="REPAIR_COMPLETE",
                failed_keyword="REPAIR_FAILED",
            )
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("repair.agent_failed", attempt=attempt, error=str(e)[:200])
            if attempt < max_attempts:
                resume = 0
                for i in range(5):
                    if checklist.steps[i]["status"] in ("pending", "running"):
                        resume = i
                        break
                await checklist.update_step(resume, f"retrying (attempt {attempt + 1})...")

    await checklist.stop()

    # Re-check health after agent
    report = await asyncio.wait_for(
        run_full_health_check(project, with_tunnel=True),
        timeout=30,
    )

    # Show final buttons — no Open App (prevents loop)
    from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
    tunnel_url = report.tunnel_url
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

        if not problems:
            await checklist.complete_step(0, f"{len(report.containers)} running")

            # Step 1: Verify app responds
            await checklist.start_step(1)
            app_ok = any(c.status == "pass" for c in report.checks if c.name == "HTTP")
            if app_ok:
                await checklist.complete_step(1, "HTTP OK")
            else:
                await checklist.complete_step(1, "⚠️ may need time")

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
            # Problems found — full agentic repair
            report = await _run_repair_loop(
                project, report, problems, chat, chat_id, message_id, [],
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
