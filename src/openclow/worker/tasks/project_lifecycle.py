"""Project lifecycle tasks — docker up/down, remove with full cleanup."""
import asyncio
import os
import shutil

from sqlalchemy import select

from openclow.models import Project, Task, async_session
from openclow.providers import factory
from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()


from openclow.services.base_reporter import edit_message as _notify  # noqa: E402


async def _load_project(project_id: int):
    """Load project from DB."""
    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        return result.scalar_one_or_none()


async def docker_up_task(ctx: dict, project_id: int, chat_id: str, message_id: str, chat_provider_type: str = "telegram"):
    """Start Docker containers + health check + tunnel + Playwright verify.

    Delegates to check_project_health which uses the full bootstrap-equivalent agent
    (_run_master_agent with Write/Edit + all Docker tools) — same power as Telegram repair.
    """
    from openclow.worker.tasks.health_task import check_project_health
    await check_project_health(
        ctx, project_id, chat_id, message_id, chat_provider_type
    )


async def docker_down_task(ctx: dict, project_id: int, chat_id: str, message_id: str, chat_provider_type: str = "telegram"):
    """Stop Docker containers for a project."""
    from openclow.services.docker_guard import run_docker
    from openclow.services.tunnel_service import stop_tunnel

    chat = await factory.get_chat_by_type(chat_provider_type)
    try:
        project = await _load_project(project_id)
        if not project:
            from openclow.providers.actions import nav_keyboard
            await _notify(chat, chat_id, message_id, "Project not found.", keyboard=nav_keyboard())
            return

        from openclow.services.checklist_reporter import ChecklistReporter
        checklist = ChecklistReporter(chat, chat_id, message_id,
                                     title=f"Stopping {project.name}")
        checklist.set_steps(["Stop containers", "Stop tunnel", "Verify stopped"])
        await checklist.start()

        workspace = os.path.join(settings.workspace_base_path, "_cache", project.name)
        compose = project.docker_compose_file or "docker-compose.yml"
        compose_project = f"openclow-{project.name}"

        # Step 1: Stop containers
        await checklist.start_step(0)
        if os.path.exists(workspace):
            rc, output = await run_docker(
                "docker", "compose", "-f", compose, "-p", compose_project,
                "down", "--remove-orphans",
                actor="lifecycle", project_id=project_id, cwd=workspace, timeout=120,
            )
            await checklist.complete_step(0, "containers stopped" if rc == 0 else f"exit code {rc}")
        else:
            await checklist.skip_step(0, "no workspace")

        # Step 2: Stop tunnel
        await checklist.start_step(1)
        try:
            await stop_tunnel(project.name)
            await checklist.complete_step(1, "tunnel stopped")
        except Exception:
            await checklist.complete_step(1, "no tunnel running")

        # Step 3: Verify
        await checklist.start_step(2)
        from openclow.services.docker_guard import run_docker as _run
        rc, _ = await _run("docker", "ps", "--filter",
                           f"label=com.docker.compose.project={compose_project}",
                           "--format", "{{.Names}}", actor="lifecycle")
        remaining = [n for n in _.strip().split("\n") if n.strip()] if rc == 0 and _.strip() else []
        if not remaining:
            await checklist.complete_step(2, "all stopped")
        else:
            await checklist.fail_step(2, f"{len(remaining)} still running")

        await checklist.stop()
        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        kb = ActionKeyboard(rows=[
            ActionRow([
                ActionButton("▶️ Docker Up", f"project_up:{project_id}"),
                ActionButton("📦 Project", f"project_detail:{project_id}"),
            ]),
            ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
        ])
        await checklist._force_render(keyboard=kb)

        log.info("lifecycle.docker_down", project=project.name)
    except asyncio.CancelledError:
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        await _notify(chat, chat_id, message_id,
                      "⚠️ Docker stop was interrupted. Containers may still be running.",
                      keyboard=project_nav_keyboard(project_id, ActionButton("Retry", f"project_down:{project_id}")))
        raise
    except Exception as e:
        log.error("lifecycle.docker_down_failed", error=str(e))
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        await _notify(chat, chat_id, message_id, f"❌ Error: {str(e)[:300]}",
                      keyboard=project_nav_keyboard(project_id, ActionButton("Retry", f"project_down:{project_id}")))


async def unlink_project_task(ctx: dict, project_id: int, chat_id: str, message_id: str, chat_provider_type: str = "telegram"):
    """Unlink a project — stop Docker + tunnel, mark inactive. Keeps workspace + DB."""
    from openclow.services.docker_guard import run_docker
    from openclow.services.tunnel_service import stop_tunnel

    chat = await factory.get_chat_by_type(chat_provider_type)
    try:
        project = await _load_project(project_id)
        if not project:
            from openclow.providers.actions import nav_keyboard
            await _notify(chat, chat_id, message_id, "Project not found.", keyboard=nav_keyboard())
            return

        project_name = project.name
        from openclow.services.checklist_reporter import ChecklistReporter
        checklist = ChecklistReporter(chat, chat_id, message_id,
                                     title=f"Unlinking {project_name}")
        checklist.set_steps(["Stop containers", "Stop tunnel", "Mark inactive"])
        await checklist.start()

        workspace = os.path.join(settings.workspace_base_path, "_cache", project_name)
        compose = project.docker_compose_file or "docker-compose.yml"
        compose_project = f"openclow-{project_name}"

        # Step 1: Stop Docker
        await checklist.start_step(0)
        if os.path.exists(workspace):
            rc, _ = await run_docker(
                "docker", "compose", "-f", compose, "-p", compose_project,
                "down", "--remove-orphans",
                actor="lifecycle", project_id=project_id, cwd=workspace, timeout=120,
            )
            await checklist.complete_step(0, "containers stopped" if rc == 0 else f"exit code {rc}")
        else:
            await checklist.complete_step(0, "no containers running")

        # Step 2: Stop tunnel
        await checklist.start_step(1)
        try:
            await stop_tunnel(project_name)
            await checklist.complete_step(1, "tunnel stopped")
        except Exception:
            await checklist.complete_step(1, "no tunnel running")

        # Step 3: Mark inactive
        await checklist.start_step(2)
        try:
            async with async_session() as session:
                result = await session.execute(select(Project).where(Project.id == project_id))
                proj = result.scalar_one_or_none()
                if proj:
                    proj.status = "inactive"
                    await session.commit()
            await checklist.complete_step(2, "marked inactive")
        except Exception as e:
            await checklist.fail_step(2, f"DB error: {str(e)[:50]}")
            log.error("lifecycle.unlink_db_failed", error=str(e))

        checklist._footer = "You can re-link it anytime to restore the project."
        await checklist.stop()
        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        kb = ActionKeyboard(rows=[
            ActionRow([
                ActionButton("🔗 Re-link Now", f"project_relink:{project_id}", style="primary"),
                ActionButton("📂 Projects", "menu:projects"),
            ]),
            ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
        ])
        await checklist._force_render(keyboard=kb)

        log.info("lifecycle.unlink_project", project=project_name)
    except asyncio.CancelledError:
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        await _notify(chat, chat_id, message_id,
                      "⚠️ Unlink was interrupted (worker restarted).\n"
                      "The project may be partially unlinked. Check its status.",
                      keyboard=project_nav_keyboard(project_id))
        raise
    except Exception as e:
        log.error("lifecycle.unlink_failed", error=str(e))
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        await _notify(chat, chat_id, message_id,
                      f"❌ Unlink failed: {str(e)[:300]}\n\nThe project is unchanged.",
                      keyboard=project_nav_keyboard(project_id))


async def remove_project_task(ctx: dict, project_id: int, chat_id: str, message_id: str, chat_provider_type: str = "telegram"):
    """Remove a project with full cleanup: Docker, tunnel, workspace, DB."""
    from openclow.services.docker_guard import run_docker
    from openclow.services.tunnel_service import stop_tunnel

    chat = await factory.get_chat_by_type(chat_provider_type)
    try:
        project = await _load_project(project_id)
        if not project:
            from openclow.providers.actions import nav_keyboard
            await _notify(chat, chat_id, message_id, "Project not found.", keyboard=nav_keyboard())
            return

        project_name = project.name

        # Check for active tasks first
        async with async_session() as session:
            active_statuses = ["pending", "preparing", "planning", "coding",
                               "reviewing", "diff_preview", "awaiting_approval", "pushing"]
            result = await session.execute(
                select(Task)
                .where(Task.project_id == project_id)
                .where(Task.status.in_(active_statuses))
                .limit(1)
            )
            active_task = result.scalar_one_or_none()
            if active_task:
                from openclow.providers.actions import ActionButton, project_nav_keyboard
                kb = project_nav_keyboard(project_id, ActionButton("Cancel Task", "menu:cancel"))
                await _notify(chat, chat_id, message_id,
                              f"❌ Cannot remove {project_name} — active task running.\n"
                              f"Cancel it first.", keyboard=kb)
                return

        from openclow.services.checklist_reporter import ChecklistReporter
        checklist = ChecklistReporter(chat, chat_id, message_id,
                                     title=f"Removing {project_name}")
        checklist.set_steps(["Stop containers", "Stop tunnel", "Delete workspace", "Delete from database"])
        await checklist.start()

        workspace = os.path.join(settings.workspace_base_path, "_cache", project_name)
        compose = project.docker_compose_file or "docker-compose.yml"
        compose_project = f"openclow-{project_name}"

        # Step 1: Stop Docker
        await checklist.start_step(0)
        if os.path.exists(workspace):
            rc, _ = await run_docker(
                "docker", "compose", "-f", compose, "-p", compose_project,
                "down", "--remove-orphans",
                actor="lifecycle", project_id=project_id, cwd=workspace, timeout=120,
            )
            await checklist.complete_step(0, "containers stopped" if rc == 0 else f"exit code {rc}")
        else:
            await checklist.complete_step(0, "no containers")

        # Step 2: Stop tunnel
        await checklist.start_step(1)
        try:
            await stop_tunnel(project_name)
            await checklist.complete_step(1, "tunnel stopped")
        except Exception:
            await checklist.complete_step(1, "no tunnel")

        # Step 3: Delete workspace
        await checklist.start_step(2)
        if os.path.exists(workspace):
            try:
                shutil.rmtree(workspace)
                await checklist.complete_step(2, "workspace deleted")
            except Exception as e:
                log.warning("lifecycle.workspace_cleanup_failed", path=workspace, error=str(e))
                await checklist.fail_step(2, f"cleanup failed: {str(e)[:50]}")
        else:
            await checklist.complete_step(2, "no workspace")

        # Step 4: Delete from DB (CASCADE handles tasks + task_logs automatically)
        await checklist.start_step(3)
        try:
            async with async_session() as session:
                result = await session.execute(select(Project).where(Project.id == project_id))
                proj = result.scalar_one_or_none()
                if proj:
                    await session.delete(proj)
                    await session.commit()
            await checklist.complete_step(3, "project + tasks + logs removed")
        except Exception as e:
            await checklist.fail_step(3, f"DB error: {str(e)[:80]}")
            log.error("lifecycle.remove_db_failed", error=str(e))

        checklist._footer = f"🗑 {project_name} permanently removed."
        await checklist.stop()
        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        kb = ActionKeyboard(rows=[
            ActionRow([
                ActionButton("➕ Add Project", "menu:addproject"),
                ActionButton("📂 Projects", "menu:projects"),
            ]),
            ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
        ])
        await checklist._force_render(keyboard=kb)

        log.info("lifecycle.remove_project", project=project_name)
    except asyncio.CancelledError:
        from openclow.providers.actions import nav_keyboard
        await _notify(chat, chat_id, message_id,
                      "⚠️ Remove was interrupted. The project may be partially removed.\nCheck Projects to verify.",
                      keyboard=nav_keyboard())
        raise
    except Exception as e:
        log.error("lifecycle.remove_failed", error=str(e))
        from openclow.providers.actions import nav_keyboard
        await _notify(chat, chat_id, message_id, f"❌ Remove failed: {str(e)[:200]}", keyboard=nav_keyboard())
