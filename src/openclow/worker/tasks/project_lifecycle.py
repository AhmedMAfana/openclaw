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
    """Start Docker containers + health check + tunnel + Playwright verify."""
    from openclow.services.docker_guard import run_docker
    from openclow.services.tunnel_service import start_tunnel
    from openclow.services.status_reporter import LineReporter as StatusReporter
    from openclow.worker.tasks.bootstrap import _step_verify_app

    chat = await factory.get_chat_by_type(chat_provider_type)
    try:
        project = await _load_project(project_id)
        if not project:
            from openclow.providers.actions import nav_keyboard
            await _notify(chat, chat_id, message_id, "Project not found.", keyboard=nav_keyboard())
            return

        workspace = os.path.join(settings.workspace_base_path, "_cache", project.name)
        compose = project.docker_compose_file or "docker-compose.yml"
        compose_project = f"openclow-{project.name}"
        from openclow.services.port_allocator import get_app_port
        port = get_app_port(project_id)

        if not os.path.exists(workspace):
            from openclow.providers.actions import ActionButton, project_nav_keyboard
            kb = project_nav_keyboard(project_id, ActionButton("Bootstrap", f"project_bootstrap:{project_id}", style="primary"))
            await _notify(chat, chat_id, message_id,
                          f"Workspace not found for {project.name}.\nRun bootstrap first.", keyboard=kb)
            return

        # Agentic: LLM agent with Docker MCP tools handles everything
        from openclow.worker.tasks._agent_helper import run_repair_agent, RepairCard

        card = RepairCard(project.name, chat, chat_id, message_id)
        await card.set_phase("repairing", "Starting Docker...")

        # Read compose + env for rich context (same as bootstrap)
        import platform
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

        prompt = (
            f"Start Docker containers and get the app running. You have FULL CONTROL.\n\n"
            f"PROJECT: {project.name}\n"
            f"WORKSPACE: {workspace}\n"
            f"COMPOSE FILE: {compose}\n"
            f"COMPOSE PROJECT: {compose_project}\n"
            f"HOST ARCHITECTURE: {platform.machine()}\n"
            f"PORT: {port}\n\n"
            f"DOCKER-COMPOSE CONTENTS:\n```yaml\n{compose_contents}\n```\n\n"
            f".ENV CONTENTS:\n```\n{env_contents}\n```\n\n"
            f"ARCHITECTURE — READ CAREFULLY:\n"
            f"- Project apps run inside Docker containers (managed by docker-compose)\n"
            f"- Cloudflare tunnels run on the WORKER HOST, NOT inside containers\n"
            f"- NEVER run 'which cloudflared' or 'cloudflared' inside containers — it does not exist there\n"
            f"- Use tunnel_start/tunnel_get_url MCP tools for tunnel management\n"
            f"- The app listens on port {port} INSIDE the container\n"
            f"- Always read container_logs BEFORE attempting fixes\n\n"
            f"STEPS:\n"
            f'1. compose_up("{compose}", "{compose_project}", "{workspace}")\n'
            f'2. compose_ps("{compose_project}") — verify all containers running\n'
            f"3. If any container down: container_logs(\"<name>\", 50) → read error → diagnose → fix → retry\n"
            f'4. docker_exec("<app_container>", "curl -sf http://localhost:{port}/ -o /dev/null -w %{{http_code}}")\n'
            f'5. tunnel_get_url("{project.name}") — if empty, tunnel_start("{project.name}", "http://<app_container_ip>:{port}")\n\n'
            f"TOOL CALLS — USE EXACT NAMES:\n"
            f'- compose_up(compose_file="{compose}", project_name="{compose_project}", working_dir="{workspace}")\n'
            f'- compose_ps(project_name="{compose_project}")\n'
            f"- container_logs(container_name=\"<name>\", tail=50)\n"
            f"- docker_exec(container_name=\"<name>\", command=\"<cmd>\")\n"
            f"- restart_container(container_name=\"<name>\")\n"
            f'- tunnel_get_url(service_name="{project.name}")\n'
            f'- tunnel_start(service_name="{project.name}", target_url="http://<container>:{port}")\n\n'
            f"DO NOT USE:\n"
            f"- No Bash tool. No shell commands on the host.\n"
            f"- No 'which cloudflared' — tunnels are NOT inside containers.\n"
            f"- No 'apt-get install' for cloudflared — it's a host service.\n\n"
            f"OUTPUT: STATUS: <step> | DIAGNOSIS: <issue> | ACTION: <fix> | FIXED: <tunnel_url>\n\n"
            f"SELF-HEALING: Read errors carefully. Fix root cause. Never give up. Try 2+ approaches per issue."
        )

        status_lines = []
        fixed = await run_repair_agent(prompt, workspace, chat, chat_id, message_id, status_lines, card=card)
        card.result_url = None  # Will be set by buttons

        # Final buttons
        from openclow.providers.actions import ActionButton, project_nav_keyboard, open_app_btn
        extra = [open_app_btn(project.id), ActionButton("Health", f"health:{project.id}")]
        kb = project_nav_keyboard(project.id, *extra)
        await chat.edit_message_with_actions(chat_id, message_id, "\n".join(status_lines)[:4000], kb)

        log.info("lifecycle.docker_up", project=project.name, fixed=fixed)
    except asyncio.CancelledError:
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        kb = project_nav_keyboard(project_id, ActionButton("Retry", f"project_up:{project_id}"))
        try:
            await chat.edit_message_with_actions(chat_id, message_id, "⏹ Docker Up cancelled.", kb)
        except Exception:
            pass
        raise
    except Exception as e:
        log.error("lifecycle.docker_up_failed", error=str(e))
        from openclow.providers.actions import ActionButton, project_nav_keyboard
        try:
            kb = project_nav_keyboard(project_id, ActionButton("Retry", f"project_up:{project_id}"))
            await chat.edit_message_with_actions(chat_id, message_id, f"❌ Error: {str(e)[:200]}", kb)
        except Exception:
            await _notify(chat, chat_id, message_id, f"❌ Error: {str(e)[:200]}")


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
