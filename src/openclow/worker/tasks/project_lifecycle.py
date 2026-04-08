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


async def docker_up_task(ctx: dict, project_id: int, chat_id: str, message_id: str):
    """Start Docker containers + health check + tunnel + Playwright verify."""
    from openclow.services.docker_guard import run_docker
    from openclow.services.tunnel_service import start_tunnel
    from openclow.services.status_reporter import LineReporter as StatusReporter
    from openclow.worker.tasks.bootstrap import _step_verify_app

    chat = await factory.get_chat()
    try:
        project = await _load_project(project_id)
        if not project:
            await _notify(chat, chat_id, message_id, "Project not found.")
            return

        workspace = os.path.join(settings.workspace_base_path, "_cache", project.name)
        compose = project.docker_compose_file or "docker-compose.yml"
        compose_project = f"openclow-{project.name}"
        from openclow.services.port_allocator import get_app_port
        port = get_app_port(project_id)

        if not os.path.exists(workspace):
            await _notify(chat, chat_id, message_id,
                          f"Workspace not found for {project.name}.\n"
                          f"Run /bootstrap {project.name} first.")
            return

        status = StatusReporter(chat, chat_id, message_id, f"Starting {project.name}")

        # 1. Docker compose up
        await status.add("🔄", "Starting Docker containers...")
        rc, output = await run_docker(
            "docker", "compose", "-f", compose, "-p", compose_project, "up", "-d",
            actor="lifecycle", project_id=project_id, cwd=workspace, timeout=120,
        )

        if rc != 0:
            # Parse error: extract meaningful message from raw Docker stderr
            error_msg = output[:300]
            # Strip timestamps and log-level noise
            import re
            clean_lines = []
            for line in error_msg.split("\n"):
                line = re.sub(r'time="[^"]*"\s*', '', line)
                line = re.sub(r'level=\w+\s*', '', line)
                line = re.sub(r'msg="([^"]*)"', r'\1', line)
                line = line.strip()
                if line and line not in clean_lines:
                    clean_lines.append(line)
            clean_error = "\n".join(clean_lines[:5]) or "Unknown error"

            await status.add("❌", f"Docker start failed:\n{clean_error}")
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            bot = chat._get_bot()
            await bot.edit_message_text(
                text=status.text()[:4000],
                chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Retry", callback_data=f"project_up:{project_id}")],
                    [InlineKeyboardButton(text="◀️ Back", callback_data=f"project_detail:{project_id}"),
                     InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
                ]),
            )
            return

        await status.add("✅", "Docker containers started", replace_last=True)

        # 2. Wait for health
        await status.add("🔄", "Waiting for containers to initialize...")
        await asyncio.sleep(10)

        # Quick health check via curl
        app_ok = False
        for attempt in range(3):
            try:
                proc = await asyncio.create_subprocess_shell(
                    f'curl -sf http://localhost:{port}/ -o /dev/null -w "%{{http_code}}" --max-time 5',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
                code = stdout.decode().strip()
                if code.startswith("2") or code.startswith("3"):
                    app_ok = True
                    break
            except Exception:
                pass
            if attempt < 2:
                await asyncio.sleep(5)

        if app_ok:
            await status.add("✅", f"App responding on port {port}", replace_last=True)
        else:
            await status.add("⚠️", f"App not responding on port {port} yet", replace_last=True)

        # 3. Start tunnel
        await status.add("🔄", "Creating public URL...")
        tunnel_url = ""
        try:
            url = await start_tunnel(project.name, f"http://localhost:{port}")
            if url:
                tunnel_url = url
                await status.add("✅", f"Public URL: {url}", replace_last=True)
            else:
                await status.add("⚠️", "Tunnel failed (local access only)", replace_last=True)
        except Exception as e:
            await status.add("⚠️", f"Tunnel error: {str(e)[:60]}", replace_last=True)

        # 4. Playwright verification
        verify_ok, verify_detail = await _step_verify_app(
            status, tunnel_url, port, project.name, workspace,
        )

        # 5. Final summary
        await status.section("Summary")
        if tunnel_url:
            status.lines.append(f"🌐 {tunnel_url}")
        if verify_ok:
            status.lines.append(f"🔍 Verified: {verify_detail[:80]}")
            status.lines.append("\n🚀 Ready!")
        elif app_ok:
            status.lines.append("\n✅ Docker running, app responding")
        else:
            status.lines.append("\n⚠️ Docker started but app may need time")

        # Final buttons
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        bot = chat._get_bot()
        buttons = [
            [InlineKeyboardButton(text="💚 Health", callback_data=f"health:{project.id}")],
        ]
        if tunnel_url:
            buttons.insert(0, [InlineKeyboardButton(text="🌐 Open App", url=tunnel_url)])

        await bot.edit_message_text(
            text=status.text()[:4000],
            chat_id=int(chat_id),
            message_id=int(message_id),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

        log.info("lifecycle.docker_up", project=project.name, rc=rc,
                 app_ok=app_ok, verified=verify_ok, tunnel=bool(tunnel_url))
    except Exception as e:
        log.error("lifecycle.docker_up_failed", error=str(e))
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        try:
            bot = chat._get_bot()
            await bot.edit_message_text(
                text=f"❌ Error: {str(e)[:200]}",
                chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Retry", callback_data=f"project_up:{project_id}")],
                    [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
                ]),
            )
        except Exception:
            await _notify(chat, chat_id, message_id, f"❌ Error: {str(e)[:200]}")
    finally:
        await chat.close()


async def docker_down_task(ctx: dict, project_id: int, chat_id: str, message_id: str):
    """Stop Docker containers for a project."""
    from openclow.services.docker_guard import run_docker
    from openclow.services.tunnel_service import stop_tunnel

    chat = await factory.get_chat()
    try:
        project = await _load_project(project_id)
        if not project:
            await _notify(chat, chat_id, message_id, "Project not found.")
            return

        workspace = os.path.join(settings.workspace_base_path, "_cache", project.name)
        compose = project.docker_compose_file or "docker-compose.yml"
        compose_project = f"openclow-{project.name}"

        await _notify(chat, chat_id, message_id, f"⏹ Stopping Docker for {project.name}...")

        rc, output = await run_docker(
            "docker", "compose", "-f", compose, "-p", compose_project,
            "down", "--remove-orphans",
            actor="lifecycle", project_id=project_id, cwd=workspace, timeout=120,
        )

        # Stop tunnel if running
        try:
            await stop_tunnel(project.name)
        except Exception:
            pass

        from aiogram.types import InlineKeyboardButton
        buttons = [
            [
                InlineKeyboardButton(text="▶️ Docker Up", callback_data=f"project_up:{project_id}"),
                InlineKeyboardButton(text="💚 Health", callback_data=f"health:{project_id}"),
            ],
            [InlineKeyboardButton(text="◀️ Projects", callback_data="menu:projects")],
        ]

        if rc == 0:
            await _notify(chat, chat_id, message_id,
                          f"✅ Docker stopped for {project.name}", buttons=buttons)
        else:
            await _notify(chat, chat_id, message_id,
                          f"⚠️ Docker stop returned code {rc} for {project.name}\n\n"
                          f"{output[:500]}", buttons=buttons)

        log.info("lifecycle.docker_down", project=project.name, rc=rc)
    except Exception as e:
        log.error("lifecycle.docker_down_failed", error=str(e))
        from aiogram.types import InlineKeyboardButton
        await _notify(chat, chat_id, message_id, f"❌ Error: {str(e)[:200]}", buttons=[
            [InlineKeyboardButton(text="🔄 Retry", callback_data=f"project_down:{project_id}")],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ])
    finally:
        await chat.close()


async def unlink_project_task(ctx: dict, project_id: int, chat_id: str, message_id: str):
    """Unlink a project — stop Docker + tunnel, mark inactive. Keeps workspace + DB."""
    from openclow.services.docker_guard import run_docker
    from openclow.services.tunnel_service import stop_tunnel

    chat = await factory.get_chat()
    try:
        project = await _load_project(project_id)
        if not project:
            await _notify(chat, chat_id, message_id, "Project not found.")
            return

        project_name = project.name
        lines = [f"🔗 Unlinking {project_name}...\n"]

        # 1. Stop Docker
        workspace = os.path.join(settings.workspace_base_path, "_cache", project_name)
        compose = project.docker_compose_file or "docker-compose.yml"
        compose_project = f"openclow-{project_name}"

        if os.path.exists(workspace):
            rc, _ = await run_docker(
                "docker", "compose", "-f", compose, "-p", compose_project,
                "down", "--remove-orphans",
                actor="lifecycle", project_id=project_id, cwd=workspace, timeout=120,
            )
            lines.append(f"{'✅' if rc == 0 else '⚠️'} Docker containers stopped")
        else:
            lines.append("⏭️ No workspace (skipping Docker)")

        # 2. Stop tunnel
        try:
            await stop_tunnel(project_name)
            lines.append("✅ Tunnel stopped")
        except Exception:
            lines.append("⏭️ No tunnel to stop")

        # 3. Mark inactive in DB
        async with async_session() as session:
            result = await session.execute(select(Project).where(Project.id == project_id))
            proj = result.scalar_one_or_none()
            if proj:
                proj.status = "inactive"
                await session.commit()
                lines.append("✅ Marked as inactive")

        lines.append(f"\n🔗 {project_name} unlinked.")

        from aiogram.types import InlineKeyboardButton
        buttons = [
            [InlineKeyboardButton(text="🔗 Re-link (Bootstrap)", callback_data=f"project_relink:{project_id}")],
            [InlineKeyboardButton(text="🗑 Remove Permanently", callback_data=f"project_remove:{project_id}")],
            [
                InlineKeyboardButton(text="➕ Add Project", callback_data="menu:addproject"),
                InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main"),
            ],
        ]
        await _notify(chat, chat_id, message_id, "\n".join(lines), buttons=buttons)

        log.info("lifecycle.unlink_project", project=project_name)
    except Exception as e:
        log.error("lifecycle.unlink_failed", error=str(e))
        await _notify(chat, chat_id, message_id, f"❌ Unlink failed: {str(e)[:200]}")
    finally:
        await chat.close()


async def remove_project_task(ctx: dict, project_id: int, chat_id: str, message_id: str):
    """Remove a project with full cleanup: Docker, tunnel, workspace, DB."""
    from openclow.services.docker_guard import run_docker
    from openclow.services.tunnel_service import stop_tunnel

    chat = await factory.get_chat()
    try:
        project = await _load_project(project_id)
        if not project:
            await _notify(chat, chat_id, message_id, "Project not found.")
            return

        project_name = project.name
        lines = [f"🗑 Removing {project_name}...\n"]

        # Check for active tasks
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
                await _notify(chat, chat_id, message_id,
                              f"❌ Cannot remove {project_name} — active task running.\n"
                              f"Cancel it first with /cancel.")
                return

        # 1. Stop Docker
        workspace = os.path.join(settings.workspace_base_path, "_cache", project_name)
        compose = project.docker_compose_file or "docker-compose.yml"
        compose_project = f"openclow-{project_name}"

        if os.path.exists(workspace):
            rc, _ = await run_docker(
                "docker", "compose", "-f", compose, "-p", compose_project,
                "down", "--remove-orphans",
                actor="lifecycle", project_id=project_id, cwd=workspace, timeout=120,
            )
            lines.append(f"{'✅' if rc == 0 else '⚠️'} Docker containers stopped")
        else:
            lines.append("⏭️ No workspace found (skipping Docker)")

        await _notify(chat, chat_id, message_id, "\n".join(lines))

        # 2. Stop tunnel
        try:
            await stop_tunnel(project_name)
            lines.append("✅ Tunnel stopped")
        except Exception:
            lines.append("⏭️ No tunnel to stop")

        # 3. Clean workspace
        if os.path.exists(workspace):
            shutil.rmtree(workspace, ignore_errors=True)
            lines.append("✅ Workspace cleaned")
        else:
            lines.append("⏭️ No workspace to clean")

        await _notify(chat, chat_id, message_id, "\n".join(lines))

        # 4. Delete from DB
        async with async_session() as session:
            result = await session.execute(select(Project).where(Project.id == project_id))
            proj = result.scalar_one_or_none()
            if proj:
                session.delete(proj)  # sync method, no await
                await session.commit()
            lines.append("✅ Removed from database")

        lines.append(f"\n🗑 {project_name} fully removed.")

        from aiogram.types import InlineKeyboardButton
        buttons = [
            [InlineKeyboardButton(text="➕ Add Project", callback_data="menu:addproject")],
            [InlineKeyboardButton(text="📂 Projects", callback_data="menu:projects")],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ]
        await _notify(chat, chat_id, message_id, "\n".join(lines), buttons=buttons)

        log.info("lifecycle.remove_project", project=project_name)
    except Exception as e:
        log.error("lifecycle.remove_failed", error=str(e))
        await _notify(chat, chat_id, message_id, f"❌ Remove failed: {str(e)[:200]}")
    finally:
        await chat.close()
