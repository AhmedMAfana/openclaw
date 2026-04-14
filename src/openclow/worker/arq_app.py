"""arq worker configuration — native asyncio task queue."""
import asyncio

from arq import create_pool
from arq.connections import RedisSettings

from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()


def parse_redis_url(url: str) -> RedisSettings:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return RedisSettings(
        host=parsed.hostname or "redis",
        port=parsed.port or 6379,
        password=parsed.password or None,
        database=int(parsed.path.lstrip("/") or 0),
    )


def _load_functions():
    from openclow.worker.tasks.orchestrator import execute_task, execute_plan, approve_task, merge_task, reject_task, discard_task
    from openclow.worker.tasks.onboarding import onboard_project, confirm_project
    from openclow.worker.tasks.chat_task import chat_response
    from openclow.worker.tasks.github_tasks import list_github_repos
    from openclow.worker.tasks.health_task import check_project_health, stop_tunnel_task
    from openclow.worker.tasks.tunnel_tasks import refresh_dashboard_tunnel, stop_dashboard_tunnel, check_tunnel_health_task
    from openclow.worker.tasks.bootstrap import bootstrap_project
    from openclow.worker.tasks.transcribe_task import transcribe_voice
    from openclow.worker.tasks.logs_task import smart_logs
    from openclow.worker.tasks.project_lifecycle import docker_up_task, docker_down_task, unlink_project_task, remove_project_task
    from openclow.worker.tasks.qa_task import run_qa_tests
    from openclow.worker.tasks.bot_lifecycle import restart_bot_task, get_bot_status_task
    from openclow.worker.tasks.agent_session import agent_session
    from openclow.worker.tasks.auth_task import claude_auth_task, claude_auth_check, claude_auth_get_url
    return [
        execute_task, execute_plan, approve_task, merge_task, reject_task, discard_task,
        onboard_project, confirm_project,
        chat_response,
        list_github_repos,
        check_project_health, stop_tunnel_task,
        refresh_dashboard_tunnel, stop_dashboard_tunnel, check_tunnel_health_task,
        bootstrap_project,
        transcribe_voice,
        smart_logs,
        docker_up_task, docker_down_task, unlink_project_task, remove_project_task,
        run_qa_tests,
        restart_bot_task,
        get_bot_status_task,
        agent_session,
        claude_auth_task,
        claude_auth_check,
        claude_auth_get_url,
    ]


async def on_startup(ctx: dict):
    """Called once when arq worker starts. Auto-start dashboard tunnel."""
    from openclow.services.tunnel_service import start_tunnel

    log.info("worker.startup", action="auto_start_dashboard_tunnel")
    url = await start_tunnel("dozzle", "http://dozzle:8080")
    if url:
        log.info("worker.dashboard_ready", url=url)
    else:
        log.warning("worker.dashboard_tunnel_failed")

    # Auto-start settings dashboard tunnel (API service)
    log.info("worker.startup", action="auto_start_settings_tunnel")
    settings_url = await start_tunnel("settings", "http://api:8000")
    if settings_url:
        log.info("worker.settings_ready", url=settings_url)
    else:
        log.warning("worker.settings_tunnel_failed")

    # Restore project tunnels from DB (they die when worker restarts)
    try:
        from sqlalchemy import select as sa_select
        from openclow.models import Project, async_session
        async with async_session() as session:
            result = await session.execute(
                sa_select(Project).where(
                    Project.status == "active",
                    Project.app_port.isnot(None),
                )
            )
            projects = result.scalars().all()
        for p in projects:
            # Find container IP for tunnel target (not host port)
            compose_project = f"openclow-{p.name}"
            try:
                from openclow.worker.tasks.bootstrap import _get_tunnel_target
                tunnel_target = await _get_tunnel_target(compose_project, f"/workspaces/_cache/{p.name}", p.id)
            except Exception:
                tunnel_target = None
            if not tunnel_target:
                from openclow.services.port_allocator import get_app_port
                tunnel_target = f"http://localhost:{get_app_port(p.id)}"
            # Get old URL before starting new tunnel
            from openclow.services.tunnel_service import get_tunnel_url
            old_url = await get_tunnel_url(p.name)

            proj_url = await start_tunnel(p.name, tunnel_target)
            if proj_url:
                log.info("worker.project_tunnel_restored", project=p.name, url=proj_url)

                # If URL changed, update .env and rebuild frontend
                if old_url and old_url != proj_url:
                    log.info("worker.tunnel_url_changed", project=p.name,
                             old=old_url, new=proj_url)
                    asyncio.create_task(
                        _sync_tunnel_url(p.name, old_url, proj_url, p.app_container_name)
                    )
    except Exception as e:
        log.warning("worker.project_tunnel_restore_failed", error=str(e))

    # Clean up orphaned Docker stacks from failed bootstraps
    # Two patterns to catch:
    # 1. "openclow-{name}-{taskid}" — old workspace_service with task ID suffix
    # 2. "{name}" (bare) — LLM agent ran `docker compose up` without -p flag
    # Legitimate stacks: "openclow-{name}" (no extra suffix)
    try:
        import subprocess
        from sqlalchemy import select as sa_select3
        from openclow.models import Project, async_session

        # Get known project names from DB
        known_projects = set()
        async with async_session() as session:
            result = await session.execute(sa_select3(Project.name))
            known_projects = {row[0] for row in result.all()}

        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", "label=com.docker.compose.project",
             "--format", "{{.Labels}}"],
            capture_output=True, text=True, timeout=10,
        )
        orphan_stacks = set()
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            for label in line.split(","):
                if "com.docker.compose.project=" in label:
                    proj = label.split("=", 1)[1]
                    # Skip our own infra stack
                    if proj == "openclow":
                        continue
                    # Pattern 1: openclow-{name}-{extra} (task ID suffix)
                    if proj.startswith("openclow-") and len(proj.split("-")) > 2:
                        orphan_stacks.add(proj)
                    # Pattern 2: bare project name (agent ran compose without -p)
                    elif proj in known_projects:
                        orphan_stacks.add(proj)

        for orphan_proj in orphan_stacks:
            log.warning("worker.cleaning_orphan_stack", project=orphan_proj)
            subprocess.run(
                ["docker", "compose", "-p", orphan_proj, "down", "--remove-orphans"],
                capture_output=True, timeout=30,
            )
    except Exception as e:
        log.warning("worker.orphan_cleanup_failed", error=str(e))

    # Recover orphaned "bootstrapping" projects — worker died mid-bootstrap
    try:
        from sqlalchemy import select as sa_select2, update as sa_update
        from openclow.models import Project, async_session
        async with async_session() as session:
            result = await session.execute(
                sa_select2(Project).where(Project.status == "bootstrapping")
            )
            orphaned = result.scalars().all()
        if orphaned:
            for p in orphaned:
                log.warning("worker.orphaned_bootstrap", project=p.name, project_id=p.id)
                async with async_session() as session:
                    await session.execute(
                        sa_update(Project)
                        .where(Project.id == p.id)
                        .values(status="failed")
                    )
                    await session.commit()
                # Release stale project lock
                try:
                    from openclow.services.project_lock import force_release
                    await force_release(p.id)
                except Exception:
                    pass
                # Notify user via chat that bootstrap was interrupted
                try:
                    from openclow.providers import factory
                    chat = await factory.get_chat()
                    # Find most recent chat_id for this project from tasks or use admin
                    from openclow.models import Task
                    async with async_session() as session:
                        task_result = await session.execute(
                            sa_select2(Task.chat_id)
                            .where(Task.project_id == p.id)
                            .where(Task.chat_id.isnot(None))
                            .order_by(Task.created_at.desc())
                            .limit(1)
                        )
                        row = task_result.first()
                    if row and row[0]:
                        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
                        kb = ActionKeyboard(rows=[
                            ActionRow([ActionButton("🔄 Retry Bootstrap", f"project_bootstrap:{p.id}")]),
                            ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
                        ])
                        await chat.send_message_with_actions(
                            row[0],
                            f"⚠️ Bootstrap for **{p.name}** was interrupted (worker restarted).\n"
                            f"The project is marked as failed. Tap Retry to try again.",
                            kb,
                        )
                except Exception as e:
                    log.warning("worker.orphan_notify_failed", error=str(e))
    except Exception as e:
        log.warning("worker.orphan_recovery_failed", error=str(e))

    # Recover orphaned tasks — stuck in intermediate states
    try:
        from openclow.models import Task
        from datetime import datetime, timedelta
        stuck_statuses = ["coding", "reviewing", "preparing", "planning", "pushing"]
        async with async_session() as session:
            cutoff = datetime.utcnow() - timedelta(minutes=30)
            result = await session.execute(
                sa_select2(Task).where(
                    Task.status.in_(stuck_statuses),
                    Task.updated_at < cutoff,
                )
            )
            stuck_tasks = result.scalars().all()
        for t in stuck_tasks:
            log.warning("worker.orphaned_task", task_id=str(t.id), status=t.status)
            async with async_session() as session:
                await session.execute(
                    sa_update(Task)
                    .where(Task.id == t.id)
                    .values(status="failed", error_message="Task interrupted — worker restarted")
                )
                await session.commit()
    except Exception as e:
        log.warning("worker.orphan_task_recovery_failed", error=str(e))

    # Pre-warm whisper model (downloads ~75MB on first use, then cached)
    asyncio.create_task(_prewarm_whisper())

    # Start periodic tunnel health monitor
    asyncio.create_task(_tunnel_health_loop())


async def _sync_tunnel_url(project_name: str, old_url: str, new_url: str, app_container_name: str | None = None):
    """Update .env files and rebuild containers when tunnel URL changes."""
    import os
    workspace = f"/workspaces/_cache/{project_name}"

    try:
        # Update .env on host workspace
        env_path = os.path.join(workspace, ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                content = f.read()
            if old_url in content:
                content = content.replace(old_url, new_url)
                with open(env_path, "w") as f:
                    f.write(content)
                log.info("worker.env_updated", project=project_name)

        # Update .env inside the app container using the project's known container name
        compose_project = f"openclow-{project_name}"
        from openclow.services.docker_guard import run_docker

        if app_container_name:
            container = f"{compose_project}-{app_container_name}-1"

            # Find all .env files in common locations and update them
            # Use sed on the container — works regardless of framework
            for env_location in [".env", "/var/www/html/.env", "/app/.env", "/src/.env"]:
                await run_docker(
                    "docker", "exec", container, "sh", "-c",
                    f"[ -f {env_location} ] && sed -i 's|{old_url}|{new_url}|g' {env_location} || true",
                    actor="tunnel_sync", timeout=10,
                )

            log.info("worker.container_env_updated", project=project_name, container=container)

        # Rebuild frontend on the host (containers mount this directory)
        # Don't run docker compose up — it can create port conflicts and duplicates.
        # The containers are already running; just rebuild static assets.
        if os.path.exists(os.path.join(workspace, "package.json")):
            log.info("worker.frontend_rebuilding", project=project_name)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "npm", "run", "build",
                    cwd=workspace,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode == 0:
                    log.info("worker.frontend_rebuilt", project=project_name)
                else:
                    log.warning("worker.frontend_rebuild_failed", project=project_name)
            except Exception as e:
                log.warning("worker.frontend_rebuild_error", error=str(e))

    except Exception as e:
        log.warning("worker.tunnel_sync_failed", project=project_name, error=str(e))


async def _prewarm_whisper():
    """Pre-download and load whisper model so first voice message is fast."""
    try:
        loop = asyncio.get_event_loop()
        from openclow.worker.tasks.transcribe_task import _get_model
        await loop.run_in_executor(None, _get_model)
        log.info("worker.whisper_ready")
    except Exception as e:
        log.warning("worker.whisper_prewarm_failed", error=str(e))


async def _tunnel_health_loop():
    """Check tunnel health every 5 minutes, restart if dead."""
    from openclow.worker.tasks.tunnel_tasks import check_tunnel_health_task

    while True:
        await asyncio.sleep(300)  # 5 minutes
        try:
            results = await check_tunnel_health_task({})
            for name, status in results.items():
                if not status.get("healthy"):
                    log.warning("tunnel.health_loop_unhealthy", service=name)
        except Exception as e:
            log.error("tunnel.health_loop_error", error=str(e))


async def on_shutdown(ctx: dict):
    """Called when arq worker shuts down. Flush pending audit entries and close DB pool."""
    from openclow.services.audit_service import flush
    await flush()
    from openclow.models.base import dispose_engine
    await dispose_engine()
    log.info("worker.shutdown", action="audit_flushed_and_engine_disposed")


class WorkerSettings:
    """arq worker settings."""
    functions = _load_functions()
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = parse_redis_url(settings.redis_url)
    max_jobs = 2
    job_timeout = 1800
    max_tries = 1  # stateful tasks (bootstrap, onboarding) must not auto-retry
    health_check_interval = 60
    allow_abort_jobs = True


async def get_arq_pool():
    return await create_pool(parse_redis_url(settings.redis_url))
