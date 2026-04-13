"""Project onboarding task — clones repo, analyzes it, saves config."""
import asyncio
import os
import shutil
import uuid

from sqlalchemy import select

from openclow.agents.onboarding import analyze_repo
from openclow.models import Project, async_session
from openclow.providers import factory
from openclow.settings import settings
from openclow.utils.logging import get_logger
from openclow.worker.tasks import git_ops

log = get_logger()


async def onboard_project(ctx: dict, repo_url: str, chat_id: str, message_id: str, chat_provider_type: str = "telegram"):
    """Clone a repo, analyze it with Claude, send config to user for approval.

    Smart cache: if this repo already exists in DB (even unlinked), skip the
    expensive clone+analyze and reuse the saved config instantly.
    """
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
            from openclow.providers.actions import ActionButton, project_nav_keyboard
            kb = project_nav_keyboard(cached_project.id)
            await chat.edit_message_with_actions(
                chat_id, message_id,
                f"Project '{cached_project.name}' is already connected and active!",
                kb,
            )
            await chat.close()
            return

        # Inactive (unlinked) — offer to re-link with cached config
        from openclow.providers.actions import ActionButton, nav_keyboard
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

    # ── No cache — full clone + analyze ──
    temp_path = os.path.join(settings.workspace_base_path, f"_onboard-{uuid.uuid4().hex[:8]}")

    from openclow.services.status_reporter import StatusReporter
    reporter = StatusReporter(chat, chat_id, message_id, title=f"Onboarding: {project_name}")
    await reporter.start()

    try:
        await reporter.stage("Cloning repository", step=1, total=5)

        # Clone
        await git.clone_repo(repo, temp_path)
        await reporter.log("Repository cloned")

        await reporter.stage("Scanning files", step=2, total=5)
        await reporter.log("Looking for docker-compose, package.json, Dockerfile...")

        await reporter.stage("Detecting tech stack", step=3, total=5)

        # Analyze with Claude (streams progress via callback)
        async def on_analysis_progress(msg: str):
            await reporter.log(msg)

        config = await analyze_repo(temp_path, on_progress=on_analysis_progress)

        if not config:
            await reporter.error("Failed to analyze project. Check the repo URL.")
            return

        await reporter.stage("Config detected", step=4, total=5)
        await reporter.log(f"Stack: {config.tech_stack}")
        if config.docker_compose:
            await reporter.log(f"Docker: {config.docker_compose}")
        if config.app_container:
            await reporter.log(f"Container: {config.app_container}:{config.app_port}")

        await reporter.stage("Ready for approval", step=5, total=5)

        # Store config temporarily for approval
        # We'll use Redis to store pending config
        import json
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        pending_key = f"openclow:pending_project:{config.name}"
        await r.set(pending_key, json.dumps({
            "name": config.name,
            "github_repo": repo,
            "tech_stack": config.tech_stack,
            "docker_compose_file": config.docker_compose,
            "app_container_name": config.app_container,
            "app_port": config.app_port,
            "description": config.description,
            "setup_commands": config.setup_commands,
            "is_dockerized": config.is_dockerized,
        }), ex=3600)  # expires in 1 hour
        await r.aclose()

        # Send config to user for approval
        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow

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
            ActionButton("Add Project", f"confirm_project:{config.name}", style="primary"),
            ActionButton("Cancel", "menu:main"),
        ])])

        await reporter.stop()
        await chat.edit_message_with_actions(chat_id, message_id, summary, kb)

        log.info("onboarding.preview_sent", project=config.name)

    except asyncio.CancelledError:
        await reporter.error("⏹ Onboarding cancelled.")
        raise
    except Exception as e:
        log.error("onboarding.failed", error=str(e), repo=repo)
        await reporter.error(f"Onboarding failed: {str(e)[:200]}")
    finally:
        await reporter.stop()
        # Cleanup temp clone
        if os.path.exists(temp_path):
            shutil.rmtree(temp_path, ignore_errors=True)


async def confirm_project(ctx: dict, project_name: str):
    """User approved — save project config to DB."""
    import json
    import redis.asyncio as aioredis

    r = aioredis.from_url(settings.redis_url)
    pending_key = f"openclow:pending_project:{project_name}"
    data_raw = await r.get(pending_key)
    await r.delete(pending_key)
    await r.aclose()

    if not data_raw:
        log.error("onboarding.confirm_no_data", project=project_name)
        return {"error": "expired", "message": "Onboarding data expired. Please re-run /addproject."}

    data = json.loads(data_raw)

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
