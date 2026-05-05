"""Project onboarding task — clones repo, analyzes it, saves config."""
import asyncio
import os
import shutil
import uuid

from sqlalchemy import select

from taghdev.agents.onboarding import analyze_repo, analyze_repo_host
from taghdev.models import Project, async_session
from taghdev.providers import factory
from taghdev.services.config_service import get_host_setting
from taghdev.settings import settings
from taghdev.utils.logging import get_logger
from taghdev.worker.tasks import git_ops

log = get_logger()


async def onboard_project(
    ctx: dict,
    repo_url: str,
    chat_id: str,
    message_id: str,
    chat_provider_type: str = "telegram",
    mode: str | None = None,
):
    """Clone a repo, analyze it with Claude, send config to user for approval.

    Smart cache: if this repo already exists in DB (even unlinked), skip the
    expensive clone+analyze and reuse the saved config instantly.

    `mode` defaults to the `host.mode_default` setting ("docker"|"host").
    In host mode the project is cloned directly into the configured
    host.projects_base (not a temp dir) and analyze_repo_host is used.
    """
    if mode is None:
        mode = (await get_host_setting("mode_default")) or "docker"

    chat = await factory.get_chat_by_type(chat_provider_type)
    git = await factory.get_git()

    # Parse repo from URL
    repo = repo_url.replace("https://github.com/", "").replace("http://github.com/", "").strip("/").replace(".git", "")
    project_name = repo.split("/")[-1] if "/" in repo else repo

    # ── Cache check: does this project already exist in DB? ──
    async with async_session() as session:
        existing = await session.execute(
            select(Project).where(
                (Project.github_repo == repo) | (Project.name == project_name)
            )
        )
        cached_project = existing.scalar_one_or_none()

    if cached_project:
        # Project exists — reuse config, skip clone+analyze
        if cached_project.status == "active":
            from taghdev.providers.actions import ActionButton, project_nav_keyboard
            kb = project_nav_keyboard(cached_project.id)
            await chat.edit_message_with_actions(
                chat_id, message_id,
                f"Project '{cached_project.name}' is already connected and active!",
                kb,
            )
            await chat.close()
            return

        # Inactive (unlinked) — offer to re-link with cached config
        from taghdev.providers.actions import ActionButton, nav_keyboard
        summary = (
            f"Project found (previously unlinked)!\n\n"
            f"Name: {cached_project.name}\n"
            f"Tech: {cached_project.tech_stack or 'unknown'}\n"
            f"Docker: {'Yes' if cached_project.is_dockerized else 'No'}\n"
            f"Description: {cached_project.description or 'N/A'}\n"
        )
        kb = nav_keyboard(ActionButton("Re-link", f"project_relink:{cached_project.id}", style="primary"))
        await chat.edit_message_with_actions(chat_id, message_id, summary, kb)
        await chat.close()
        return

    # ── Host-mode onboarding: clone into the configured projects base, not a temp dir ──
    if mode == "host":
        return await _onboard_project_host(
            repo=repo, project_name=project_name,
            chat=chat, chat_id=chat_id, message_id=message_id,
        )

    # ── No cache — full clone + analyze ──
    temp_path = os.path.join(settings.workspace_base_path, f"_onboard-{uuid.uuid4().hex[:8]}")

    from taghdev.services.checklist_reporter import ChecklistReporter
    reporter = ChecklistReporter(chat, chat_id, message_id, title=f"Adding {project_name}")
    reporter.set_steps(["Clone repository", "Scan files", "Detect tech stack", "Review config", "Confirm"])
    await reporter.start()

    try:
        await reporter.start_step(0)

        # Clone
        await git.clone_repo(repo, temp_path)
        await reporter.complete_step(0, "cloned")

        await reporter.start_step(1)
        await reporter.update_step(1, "looking for docker-compose, package.json...")

        await reporter.start_step(2)

        # Analyze with Claude (streams progress via callback)
        async def on_analysis_progress(msg: str):
            await reporter.log(msg)

        config = await analyze_repo(temp_path, on_progress=on_analysis_progress)

        if not config:
            await reporter.fail_step(2, "analysis failed")
            reporter._footer = "Failed to analyze project. Check the repo URL."
            await reporter._force_render()
            return

        await reporter.complete_step(2, config.tech_stack or "detected")
        await reporter.start_step(3)
        detail = config.app_container or config.docker_compose or ""
        await reporter.complete_step(3, detail[:40] if detail else "reviewed")
        await reporter.start_step(4)
        await reporter.complete_step(4, "ready")

        # Store config temporarily for approval
        # We'll use Redis to store pending config
        import json
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        # Always key by repo name (predictable), also store repo_name alias key
        # config.name = AI-detected app name (can differ from repo slug)
        # project_name = last segment of GitHub repo URL (always matches what agent searches)
        # Key by repo slug (predictable — matches what agent and user call the project).
        # Also write under AI-detected name as alias so Telegram/Slack button callbacks work.
        pending_key = f"taghdev:pending_project:{project_name}"
        alt_key = f"taghdev:pending_project:{config.name}"
        payload = json.dumps({
            "name": project_name,
            "github_repo": repo,
            "tech_stack": config.tech_stack,
            "docker_compose_file": config.docker_compose,
            "app_container_name": config.app_container,
            "app_port": config.app_port,
            "description": config.description,
            "setup_commands": config.setup_commands,
            "is_dockerized": config.is_dockerized,
        })
        await r.set(pending_key, payload, ex=3600)
        if alt_key != pending_key:
            await r.set(alt_key, payload, ex=3600)
        await r.aclose()

        # Send config to user for approval
        from taghdev.providers.actions import ActionButton, ActionKeyboard, ActionRow

        summary = (
            f"Project analyzed!\n\n"
            f"Name: {config.name}\n"
            f"Tech: {config.tech_stack}\n"
            f"Docker: {'Yes' if config.is_dockerized else 'No'}\n"
        )
        if config.docker_compose:
            summary += f"Compose: {config.docker_compose}\n"
        if config.app_container:
            summary += f"Container: {config.app_container}\n"
        if config.app_port:
            summary += f"Port: {config.app_port}\n"
        summary += f"Description: {config.description}\n"
        if config.setup_commands:
            summary += f"Setup: {config.setup_commands}\n"

        kb = ActionKeyboard(rows=[ActionRow([
            ActionButton("Add Project", f"confirm_project:{project_name}", style="primary"),
            ActionButton("Cancel", "menu:main"),
        ])])

        await reporter.stop()
        await chat.edit_message_with_actions(chat_id, message_id, summary, kb)

        log.info("onboarding.preview_sent", project=config.name)

    except asyncio.CancelledError:
        running_idx = next((i for i, s in enumerate(reporter.steps) if s["status"] == "running"), 0)
        await reporter.fail_step(running_idx, "cancelled")
        reporter._footer = "Onboarding cancelled."
        await reporter._force_render()
        raise
    except Exception as e:
        log.error("onboarding.failed", error=str(e), repo=repo)
        running_idx = next((i for i, s in enumerate(reporter.steps) if s["status"] == "running"), 0)
        await reporter.fail_step(running_idx, str(e)[:40])
        reporter._footer = f"Failed: {str(e)[:200]}"
        await reporter._force_render()
    finally:
        await reporter.stop()
        # Cleanup temp clone
        if os.path.exists(temp_path):
            shutil.rmtree(temp_path, ignore_errors=True)


async def _onboard_project_host(
    *, repo: str, project_name: str, chat, chat_id: str, message_id: str,
):
    """Host-mode onboarding: clone directly into host.projects_base/<name>
    (if missing and auto_clone is on), git pull, then run analyze_repo_host."""
    import json

    import redis.asyncio as aioredis

    from taghdev.services.checklist_reporter import ChecklistReporter
    from taghdev.services.host_guard import run_host

    base = await get_host_setting("projects_base")
    auto_clone = await get_host_setting("auto_clone")
    project_dir = os.path.join(base or "/srv/projects", project_name)

    reporter = ChecklistReporter(chat, chat_id, message_id, title=f"Adding {project_name}")
    reporter.set_steps([
        "Locate project dir", "Sync latest code", "Read install guide",
        "Detect tech + start command", "Confirm",
    ])
    await reporter.start()

    try:
        await reporter.start_step(0)

        if not os.path.isdir(project_dir):
            if not auto_clone:
                await reporter.fail_step(
                    0, f"dir missing and auto_clone off: {project_dir}"
                )
                reporter._footer = (
                    f"Project dir {project_dir} does not exist. "
                    "Clone it manually on the host or enable auto_clone in settings."
                )
                await reporter._force_render()
                return

            # Auto-clone using git CLI directly (no host_guard here — we need to
            # create the parent dir, which is always inside host.projects_base).
            parent = os.path.dirname(project_dir)
            os.makedirs(parent, exist_ok=True)
            clone_url = f"https://github.com/{repo}.git" if "/" in repo and "://" not in repo else repo
            rc, out = await run_host(
                f"git clone {clone_url} {os.path.basename(project_dir)}",
                cwd=parent, actor="onboarding", timeout=300, project_name=project_name,
            )
            if rc != 0:
                await reporter.fail_step(0, f"clone failed")
                reporter._footer = f"git clone failed:\n{out[-400:]}"
                await reporter._force_render()
                return
            await reporter.complete_step(0, f"cloned into {project_dir}")
        else:
            await reporter.complete_step(0, project_dir)

        await reporter.start_step(1)
        rc, out = await run_host(
            "git fetch origin && git log -1 --oneline",
            cwd=project_dir, actor="onboarding", timeout=30, project_name=project_name,
        )
        await reporter.complete_step(1, (out.splitlines() or [""])[0][-60:] if rc == 0 else "fetched (no origin)")

        await reporter.start_step(2)

        async def on_progress(msg: str):
            await reporter.log(msg)

        config = await analyze_repo_host(project_dir, on_progress=on_progress)

        if not config:
            await reporter.fail_step(2, "analysis failed")
            reporter._footer = "Failed to analyze project. Check the install guide exists."
            await reporter._force_render()
            return

        await reporter.complete_step(2, config.install_guide_path or "ok")
        await reporter.start_step(3)
        detail = config.start_command or config.tech_stack or ""
        await reporter.complete_step(3, detail[:50] if detail else "detected")
        await reporter.start_step(4)
        await reporter.complete_step(4, "ready")

        # Stash the detected config in Redis so confirm_project can persist it.
        r = aioredis.from_url(settings.redis_url)
        pending_key = f"taghdev:pending_project:{project_name}"
        alt_key = f"taghdev:pending_project:{config.name}"
        payload = json.dumps({
            "mode": "host",
            "name": project_name,
            "github_repo": repo,
            "tech_stack": config.tech_stack,
            "description": config.description,
            "setup_commands": config.setup_commands,
            "is_dockerized": False,
            "app_port": config.app_port,
            # host-mode fields
            "project_dir": project_dir,
            "install_guide_path": config.install_guide_path,
            "start_command": config.start_command,
            "stop_command": config.stop_command,
            "health_url": config.health_url,
            "process_manager": config.process_manager,
        })
        await r.set(pending_key, payload, ex=3600)
        if alt_key != pending_key:
            await r.set(alt_key, payload, ex=3600)
        await r.aclose()

        from taghdev.providers.actions import ActionButton, ActionKeyboard, ActionRow

        summary_lines = [
            f"Host-mode project analyzed!",
            f"",
            f"Name: {project_name}",
            f"Path: {project_dir}",
            f"Tech: {config.tech_stack}",
        ]
        if config.install_guide_path:
            summary_lines.append(f"Guide: {config.install_guide_path}")
        if config.start_command:
            summary_lines.append(f"Start: {config.start_command[:80]}")
        if config.app_port:
            summary_lines.append(f"Port: {config.app_port}")
        if config.health_url:
            summary_lines.append(f"Health: {config.health_url}")
        if config.process_manager and config.process_manager != "manual":
            summary_lines.append(f"Manager: {config.process_manager}")
        if config.setup_commands:
            summary_lines.append(f"Setup: {config.setup_commands[:120]}")
        summary_lines += ["", config.description or ""]

        kb = ActionKeyboard(rows=[ActionRow([
            ActionButton("Add Project", f"confirm_project:{project_name}", style="primary"),
            ActionButton("Cancel", "menu:main"),
        ])])

        await reporter.stop()
        await chat.edit_message_with_actions(chat_id, message_id, "\n".join(summary_lines), kb)
        log.info("onboarding.host_preview_sent", project=project_name, path=project_dir)

    except asyncio.CancelledError:
        running_idx = next((i for i, s in enumerate(reporter.steps) if s["status"] == "running"), 0)
        await reporter.fail_step(running_idx, "cancelled")
        reporter._footer = "Onboarding cancelled."
        await reporter._force_render()
        raise
    except Exception as e:
        log.error("onboarding.host_failed", error=str(e), project=project_name)
        running_idx = next((i for i, s in enumerate(reporter.steps) if s["status"] == "running"), 0)
        await reporter.fail_step(running_idx, str(e)[:40])
        reporter._footer = f"Failed: {str(e)[:200]}"
        await reporter._force_render()
    finally:
        await reporter.stop()


async def confirm_project(ctx: dict, project_name: str):
    """User approved — save project config to DB."""
    import json
    import redis.asyncio as aioredis

    r = aioredis.from_url(settings.redis_url)
    pending_key = f"taghdev:pending_project:{project_name}"
    data_raw = await r.get(pending_key)
    await r.delete(pending_key)
    await r.aclose()

    if not data_raw:
        log.error("onboarding.confirm_no_data", project=project_name)
        return {"error": "expired", "message": "Onboarding data expired. Please re-run /addproject."}

    data = json.loads(data_raw)
    mode = data.get("mode", "docker")

    from sqlalchemy.exc import IntegrityError

    async with async_session() as session:
        project = Project(
            name=data["name"],
            github_repo=data["github_repo"],
            default_branch="main",
            tech_stack=data.get("tech_stack"),
            description=data.get("description"),
            is_dockerized=data.get("is_dockerized", True),
            docker_compose_file=data.get("docker_compose_file"),
            app_container_name=data.get("app_container_name"),
            app_port=data.get("app_port"),
            setup_commands=data.get("setup_commands"),
            # Host-mode fields — NULL for Docker-mode projects
            mode=mode,
            project_dir=data.get("project_dir"),
            install_guide_path=data.get("install_guide_path"),
            start_command=data.get("start_command"),
            stop_command=data.get("stop_command"),
            health_url=data.get("health_url"),
            process_manager=data.get("process_manager"),
            status="bootstrapping",
        )
        session.add(project)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            log.warning("onboarding.duplicate_project", project=project_name)
            return {"error": "duplicate", "message": f"Project '{data['name']}' already exists."}
        except Exception as e:
            await session.rollback()
            log.error("onboarding.db_error", error=str(e))
            return {"error": str(e)[:200]}
        project_id = project.id

    log.info("onboarding.confirmed", project=project_name, project_id=project_id)
    return project_id  # used by admin handler to trigger bootstrap
