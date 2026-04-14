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

REPAIR_AGENT_PROMPT = """You are fixing a running project's Docker issues. You have FULL CONTROL.

PROJECT: {project_name}
WORKSPACE: {workspace}
COMPOSE FILE: {compose_file}
COMPOSE PROJECT: {compose_project}
HOST ARCHITECTURE: {arch}
PORT: {port}

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

YOUR GOAL: Get ALL containers running + tunnel URL working.

ARCHITECTURE — READ CAREFULLY:
- The project runs inside Docker containers (managed by docker-compose)
- Cloudflare tunnels run on the WORKER HOST, NOT inside containers
- You manage tunnels via tunnel_start/tunnel_get_url MCP tools
- NEVER run "which cloudflared" or "cloudflared" inside containers — it does not exist there
- The app listens on port {port} INSIDE the container

STEPS:
1. compose_ps("{compose_project}") — see current state
2. container_logs("<container_name>", 50) — read logs for EVERY non-running container
3. If containers down/exited:
   - Read the logs FIRST to understand why
   - DIAGNOSIS: <what the logs say>
   - If config issue: Edit the file in workspace ({workspace})
   - compose_up("{compose_file}", "{compose_project}", "{workspace}")
4. If containers running but app not responding:
   - docker_exec("<app_container>", "curl -sf http://localhost:{port}/ -o /dev/null -w %{{http_code}}")
   - If 000/error: check logs, fix app config, restart container
5. tunnel_get_url("{project_name}") — check if tunnel exists
6. If no tunnel: tunnel_start("{project_name}", "http://<app_container_ip>:{port}")

TOOL CALLS — USE EXACT NAMES:
- compose_ps(compose_project="{compose_project}")
- compose_up(compose_file="{compose_file}", compose_project="{compose_project}", workspace="{workspace}")
- container_logs(container_name="<name>", tail=50)
- docker_exec(container_name="<name>", command="<cmd>")
- restart_container(container_name="<name>")
- tunnel_get_url(service_name="{project_name}")
- tunnel_start(service_name="{project_name}", target_url="http://<container>:{port}")
- Read/Edit/Glob for workspace files at {workspace}

DO NOT USE:
- No Bash tool. No shell commands on the host.
- No "which cloudflared" — tunnels are NOT inside containers.
- No "apt-get install" for cloudflared — it's a host service.

OUTPUT FORMAT:
- STATUS: <what you're doing>
- DIAGNOSIS: <what's wrong>
- ACTION: <what you're fixing>
- FIXED: <tunnel_url> — when everything works

SELF-HEALING:
- Read error messages carefully before acting
- Fix the root cause, not symptoms
- NEVER give up after first failure
- Try at least 2 different approaches per issue
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
    """Agentic repair loop — uses shared run_repair_agent + RepairCard."""
    from openclow.settings import settings
    from openclow.worker.tasks._agent_helper import run_repair_agent

    workspace = os.path.join(settings.workspace_base_path, "_cache", project.name)
    compose = project.docker_compose_file or "docker-compose.yml"
    compose_project = f"openclow-{project.name}"

    if not os.path.exists(workspace):
        if card:
            await card.set_status("⚠️ Workspace not found — run bootstrap first")
        return report

    # Read compose file contents — same as bootstrap
    compose_path = os.path.join(workspace, compose)
    compose_contents = ""
    if os.path.exists(compose_path):
        with open(compose_path) as f:
            compose_contents = f.read()[:4000]

    # Read .env
    env_path = os.path.join(workspace, ".env")
    env_contents = ""
    if os.path.exists(env_path):
        with open(env_path) as f:
            env_contents = f.read()[:2000]

    # Container names from report
    container_status = "\n".join(
        f"- {c.name}: {c.state} {c.health}" for c in report.containers
    ) or "No containers found"

    # Architecture + port
    import platform
    arch = platform.machine()
    from openclow.services.port_allocator import get_app_port
    port = get_app_port(project.id)

    problems_text = "\n".join(f"- {p['detail']}" for p in problems)

    prompt = REPAIR_AGENT_PROMPT.format(
        project_name=project.name,
        workspace=workspace,
        compose_file=compose,
        compose_project=compose_project,
        arch=arch,
        port=port,
        compose_contents=compose_contents,
        env_contents=env_contents,
        container_status=container_status,
        problems_text=problems_text,
    )

    await run_repair_agent(prompt, workspace, chat, chat_id, message_id, status_lines, card=card)

    # Re-check health
    if card:
        await card.set_status("Re-checking health...")
    await asyncio.sleep(3)
    report = await asyncio.wait_for(
        run_full_health_check(project, with_tunnel=True),
        timeout=30,
    )
    return report


# ---------------------------------------------------------------------------
# Main worker tasks
# ---------------------------------------------------------------------------

async def check_project_health(ctx: dict, project_id: int, chat_id: str, message_id: str, chat_provider_type: str = "telegram"):
    """Worker task: ONE smooth card from start to finish — check → repair → done."""
    from openclow.models import Project, async_session
    from openclow.providers import factory
    from openclow.worker.tasks._agent_helper import RepairCard
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

        # ONE card for the entire flow
        card = RepairCard(project.name, chat, chat_id, message_id)
        await card.set_phase("checking", "Running health check...")

        # ── Phase 1: Health check ──
        report = await asyncio.wait_for(
            run_full_health_check(project, with_tunnel=True),
            timeout=30,
        )

        problems = _find_problems(report)

        if not problems:
            # All good — show final card
            card.result_url = report.tunnel_url
            await card.set_phase("done", "Everything healthy!")
            await card.complete_activity(f"{len(report.containers)} containers running")
            if report.tunnel_url:
                await card.complete_activity("Tunnel active")
        else:
            # Show what's wrong, then repair — retry up to 3 times
            max_repair_attempts = 3
            for repair_attempt in range(1, max_repair_attempts + 1):
                for p in problems[:3]:
                    await card.complete_activity(f"❌ {p['detail'][:50]}")

                attempt_label = f" (attempt {repair_attempt}/{max_repair_attempts})" if repair_attempt > 1 else ""
                await card.set_phase("repairing", f"Fixing {len(problems)} issue(s){attempt_label}...")

                status_lines = []
                report = await _run_repair_loop(
                    project, report, problems, chat, chat_id, message_id, status_lines,
                    card=card,
                )

                card.result_url = report.tunnel_url
                new_problems = _find_problems(report)
                if not new_problems:
                    await card.set_phase("done", "All issues fixed!")
                    break

                if repair_attempt < max_repair_attempts:
                    problems = new_problems
                    await card.set_status(f"{len(new_problems)} issue(s) remain — retrying...")
                else:
                    await card.set_phase("done", f"{len(new_problems)} issue(s) remain after {max_repair_attempts} attempts")

        # ── Always: final buttons ──
        await _notify_with_buttons(chat, chat_id, message_id,
                                   card._render(), project_id, tunnel_url=report.tunnel_url)

        log.info("health.check_done", project=project.name, running=report.is_running,
                 problems=len(problems), tunnel=report.tunnel_url is not None)

    except asyncio.TimeoutError:
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        kb = project_nav_keyboard(project_id,
            ActionButton("🔄 Retry", f"health:{project_id}", style="primary"))
        await _notify(chat, chat_id, message_id, f"🔧 *{project.name}*\n\n⏱ Timed out — tap Retry or ask Agent", keyboard=kb)
    except asyncio.CancelledError:
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        kb = project_nav_keyboard(project_id,
            ActionButton("🔄 Retry", f"health:{project_id}", style="primary"))
        await _notify(chat, chat_id, message_id, f"🔧 *{project.name}*\n\n⏹ Cancelled", keyboard=kb)
        raise
    except Exception as e:
        log.error("health.check_failed", error=str(e))
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        kb = project_nav_keyboard(project_id,
            ActionButton("🔄 Retry", f"health:{project_id}", style="primary"))
        await _notify(chat, chat_id, message_id, f"🔧 *{project.name}*\n\n❌ {str(e)[:100]}", keyboard=kb)


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
