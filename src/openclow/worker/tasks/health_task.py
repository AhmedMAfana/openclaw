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
    """Format a HealthReport as a Telegram message."""
    lines = [f"📊 {report.project_name} — Health Check\n"]

    if not report.is_running:
        lines.append("❌ No running containers found.\n")
        for c in report.checks:
            icon = STATUS_ICONS.get(c.status, "❓")
            lines.append(f"  {icon} {c.name}: {c.detail}")
        return "\n".join(lines)

    # Containers
    lines.append("Docker Containers:")
    for c in report.containers:
        if c.state == "running":
            icon = "✅"
        elif c.state == "exited":
            icon = "❌"
        else:
            icon = "⚠️"
        import re
        short_name = c.name.split("-")[-1] if "-" in c.name else c.name
        port_str = ""
        if c.ports:
            port_match = re.search(r"0\.0\.0\.0:(\d+)->", c.ports)
            if port_match:
                port_str = f" → :{port_match.group(1)}"
        health_str = ""
        if "healthy" in c.health.lower():
            health_str = " (healthy)"
        elif "unhealthy" in c.health.lower():
            health_str = " (unhealthy)"
        lines.append(f"  {icon} {short_name}: {c.state}{health_str}{port_str}")

    # Health checks
    lines.append("\nHealth Checks:")
    for c in report.checks:
        icon = STATUS_ICONS.get(c.status, "❓")
        lines.append(f"  {icon} {c.name}: {c.detail}")

    # Tunnel URL
    if report.tunnel_url:
        lines.append(f"\n🔗 {report.tunnel_url}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram update helpers
# ---------------------------------------------------------------------------

from openclow.services.base_reporter import edit_message as _notify  # noqa: E402


async def _notify_with_buttons(chat, chat_id, message_id, text, project_id, tunnel_url=None):
    """Update Telegram message with health-check action buttons."""
    from aiogram.types import InlineKeyboardButton

    buttons = []
    if tunnel_url:
        buttons.append([InlineKeyboardButton(text="🌐 Open App", url=tunnel_url)])
    buttons.append([
        InlineKeyboardButton(text="🔄 Refresh", callback_data=f"health_ref:{project_id}"),
    ])
    if tunnel_url:
        buttons.append([
            InlineKeyboardButton(text="⏹ Stop Tunnel", callback_data=f"tunnel_stop:{project_id}"),
        ])
    buttons.append([
        InlineKeyboardButton(text="◀️ Back", callback_data=f"project_detail:{project_id}"),
        InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"),
    ])

    await _notify(chat, chat_id, message_id, text, buttons=buttons)


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

async def _run_repair_loop(
    project,
    report: HealthReport,
    problems: list[dict],
    chat,
    chat_id: str,
    message_id: str,
    status_lines: list[str],
):
    """Run the repair loop for detected problems.

    This is the heart of the agentic health check:
    - For each broken container: call Doctor to diagnose + fix
    - Report every step to Telegram
    - Re-check after repairs
    - Give up gracefully with clear explanation if can't fix
    """
    from openclow.agents.doctor import repair_container
    from openclow.settings import settings

    workspace = os.path.join(settings.workspace_base_path, "_cache", project.name)
    compose = project.docker_compose_file or "docker-compose.yml"
    compose_project = f"openclow-{project.name}"

    if not os.path.exists(workspace):
        status_lines.append("⚠️ Workspace not found — cannot auto-repair")
        status_lines.append(f"   Run /bootstrap to set up {project.name} first")
        await _notify(chat, chat_id, message_id, "\n".join(status_lines))
        return report

    # ── Group problems ──
    container_problems = [p for p in problems if p["type"] in ("container_down", "container_unhealthy")]
    check_problems = [p for p in problems if p["type"] == "check_failed"]

    repaired_any = False

    # ── Fix containers first ──
    if container_problems:
        status_lines.append("")
        status_lines.append("🔧 Auto-Repair Mode")
        status_lines.append("─" * 25)
        await _notify(chat, chat_id, message_id, "\n".join(status_lines))

        for problem in container_problems:
            container_name = problem["container"]

            # Progress callback — updates Telegram in real-time
            async def on_progress(icon: str, msg: str, _cn=container_name):
                status_lines.append(f"  {icon} [{_cn.split('-')[-1]}] {msg}")
                await _notify(chat, chat_id, message_id, "\n".join(status_lines))

            repair_report = await repair_container(
                container=container_name,
                workspace=workspace,
                compose_file=compose,
                compose_project=compose_project,
                max_attempts=3,
                on_progress=on_progress,
            )

            if repair_report.fixed:
                repaired_any = True
                status_lines.append(f"  ✅ {container_name.split('-')[-1]}: {repair_report.final_status[:80]}")
            else:
                status_lines.append(f"  ❌ {container_name.split('-')[-1]}: could not auto-repair")
                if repair_report.suggestion:
                    status_lines.append(f"     → {repair_report.suggestion[:120]}")

            await _notify(chat, chat_id, message_id, "\n".join(status_lines))

    # ── If we repaired containers, re-run health checks ──
    if repaired_any:
        status_lines.append("")
        status_lines.append("🔄 Re-checking health after repairs...")
        await _notify(chat, chat_id, message_id, "\n".join(status_lines))

        await asyncio.sleep(5)
        report = await asyncio.wait_for(
            run_full_health_check(project, with_tunnel=True),
            timeout=30,
        )

        new_problems = _find_problems(report)
        if not new_problems:
            status_lines.append("✅ All issues resolved!")
        else:
            remaining = [p["detail"] for p in new_problems]
            status_lines.append(f"⚠️ {len(new_problems)} issue(s) remain:")
            for r in remaining[:5]:
                status_lines.append(f"  • {r[:80]}")

        await _notify(chat, chat_id, message_id, "\n".join(status_lines))

    # ── Handle check-only failures (HTTP not responding, DB down) ──
    elif check_problems and not container_problems:
        status_lines.append("")
        status_lines.append("⚠️ Services are running but checks failing:")
        for cp in check_problems:
            status_lines.append(f"  • {cp['name']}: {cp['detail']}")
        status_lines.append("")
        status_lines.append("This usually means:")
        status_lines.append("  • App is still starting up (wait 30s, refresh)")
        status_lines.append("  • App crashed after start (check logs)")
        status_lines.append("  • Port mismatch in project config")
        await _notify(chat, chat_id, message_id, "\n".join(status_lines))

    return report


# ---------------------------------------------------------------------------
# Main worker tasks
# ---------------------------------------------------------------------------

async def check_project_health(ctx: dict, project_id: int, chat_id: str, message_id: str):
    """Worker task: run health check → auto-repair if broken → report to Telegram."""
    from openclow.models import Project, async_session
    from openclow.providers import factory
    from sqlalchemy import select

    chat = await factory.get_chat()

    try:
        # Load project (expunge so it's usable after session closes)
        async with async_session() as session:
            result = await session.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if project:
                session.expunge(project)

        if not project:
            await chat.edit_message(chat_id, message_id, "Project not found.")
            return

        status_lines = [f"🔍 Checking {project.name}..."]
        await _notify(chat, chat_id, message_id, "\n".join(status_lines))

        # ── Phase 1: Health check ──
        report = await asyncio.wait_for(
            run_full_health_check(project, with_tunnel=True),
            timeout=30,
        )

        # Show initial report
        status_lines = [format_health_report(report)]

        # ── Phase 2: Detect problems ──
        problems = _find_problems(report)

        if problems:
            # Show what we found, then start repair
            status_lines.append(f"\n🚨 Found {len(problems)} issue(s) — starting auto-repair...")
            await _notify(chat, chat_id, message_id, "\n".join(status_lines))

            # ── Phase 3: Repair loop ──
            report = await _run_repair_loop(
                project, report, problems, chat, chat_id, message_id, status_lines,
            )

            # Rebuild the display with updated report (single report, no duplication)
            final_text = format_health_report(report)
        else:
            final_text = "\n".join(status_lines)
            final_text += "\n\n✅ Everything looks healthy!"

        # ── Final display with buttons ──
        await _notify_with_buttons(
            chat, chat_id, message_id,
            final_text, project_id,
            tunnel_url=report.tunnel_url,
        )

        log.info("health.check_done", project=project.name, running=report.is_running,
                 problems=len(problems), tunnel=report.tunnel_url is not None)

    except asyncio.TimeoutError:
        from aiogram.types import InlineKeyboardButton
        await _notify(chat, chat_id, message_id, "Health check timed out.", buttons=[
            [InlineKeyboardButton(text="🔄 Retry", callback_data=f"health:{project_id}")],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ])
    except Exception as e:
        log.error("health.check_failed", error=str(e))
        from aiogram.types import InlineKeyboardButton
        await _notify(chat, chat_id, message_id, f"Health check failed: {str(e)[:150]}", buttons=[
            [InlineKeyboardButton(text="🔄 Retry", callback_data=f"health:{project_id}")],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ])
    finally:
        await chat.close()


async def stop_tunnel_task(ctx: dict, project_id: int, chat_id: str, message_id: str):
    """Worker task: stop a running tunnel for a project."""
    from openclow.models import Project, async_session
    from openclow.providers import factory
    from sqlalchemy import select

    chat = await factory.get_chat()

    try:
        async with async_session() as session:
            result = await session.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if project:
                session.expunge(project)

        if project:
            await _stop_tunnel(project.name)
            await _notify(chat, chat_id, message_id, f"Tunnel stopped for {project.name}.")
        else:
            await _notify(chat, chat_id, message_id, "Project not found.")
    except Exception as e:
        log.error("health.stop_tunnel_failed", error=str(e))
    finally:
        await chat.close()
