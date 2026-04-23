"""arq worker configuration — native asyncio task queue."""
import asyncio

from arq import create_pool, cron
from arq.connections import RedisSettings

from openclow.settings import settings
from openclow.utils.docker_path import get_docker_env
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
    from openclow.worker.tasks.auth_task import claude_auth_task, claude_auth_check, claude_auth_get_url, claude_auth_login_web
    from openclow.worker.tasks.instance_tasks import provision_instance, teardown_instance
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
        claude_auth_login_web,
        provision_instance,
        teardown_instance,
    ]


async def on_startup(ctx: dict):
    """Called once when arq worker starts. Auto-start dashboard tunnel."""
    from openclow.services.tunnel_service import start_tunnel

    # Start infra tunnels in BACKGROUND — never block worker startup.
    # Worker must be ready to process jobs immediately.
    asyncio.create_task(_start_infra_tunnels())

    # Restore project tunnels — any active project with a running container
    # gets its public URL back immediately after worker restart.
    asyncio.create_task(_restore_project_tunnels())

    # Clean up orphaned Docker stacks from failed bootstraps
    # Two patterns to catch:
    # 1. "openclow-{name}-{taskid}" — old workspace_service with task ID suffix
    # 2. "{name}" (bare) — LLM agent ran `docker compose up` without -p flag
    # Legitimate stacks: "openclow-{name}" (no extra suffix)
    try:
        from sqlalchemy import select as sa_select3
        from openclow.models import Project, async_session

        # Get known project names from DB
        known_projects = set()
        async with async_session() as session:
            result = await session.execute(sa_select3(Project.name))
            known_projects = {row[0] for row in result.all()}

        _denv = get_docker_env()
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--filter", "label=com.docker.compose.project",
            "--format", "{{.Labels}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_denv,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        # Build set of legitimate stack names: "openclow-{project_name}"
        legitimate = {"openclow"} | {f"openclow-{name}" for name in known_projects}

        orphan_stacks = set()
        for line in stdout.decode().strip().split("\n"):
            if not line:
                continue
            for label in line.split(","):
                if "com.docker.compose.project=" in label:
                    proj = label.split("=", 1)[1]
                    if proj in legitimate:
                        continue
                    if proj.startswith("openclow-"):
                        orphan_stacks.add(proj)
                    elif proj in known_projects:
                        orphan_stacks.add(proj)

        for orphan_proj in orphan_stacks:
            log.warning("worker.cleaning_orphan_stack", project=orphan_proj)
            orphan_proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "-p", orphan_proj, "down", "--remove-orphans",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=_denv,
            )
            await asyncio.wait_for(orphan_proc.communicate(), timeout=30)
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
                sa_select2(Task.id, Task.status, Task.updated_at, Task.project_id).where(
                    Task.status.in_(stuck_statuses),
                    Task.updated_at < cutoff,
                )
            )
            stuck_tasks = result.all()
        for t in stuck_tasks:
            log.warning("worker.orphaned_task", task_id=str(t[0]), status=t[1])
            async with async_session() as session:
                await session.execute(
                    sa_update(Task)
                    .where(Task.id == t[0])
                    .values(status="failed", error_message="Task interrupted — worker restarted")
                )
                await session.commit()
        # Release project locks held by stuck tasks
        released_projects: set[int] = set()
        for t in stuck_tasks:
            project_id = t[3]  # Task.project_id
            if project_id and project_id not in released_projects:
                try:
                    from openclow.services.project_lock import force_release
                    await force_release(project_id)
                    released_projects.add(project_id)
                    log.info("worker.stale_lock_released", project_id=project_id, task_id=str(t[0]))
                except Exception:
                    pass
        # Finalize web progress cards so the UI shows red/failed
        if stuck_tasks:
            from openclow.worker.tasks.maintenance import _finalize_web_progress_cards
            await _finalize_web_progress_cards(stuck_tasks, "Task interrupted — worker restarted")
    except Exception as e:
        log.warning("worker.orphan_task_recovery_failed", error=str(e))

    # Release ALL stale project locks on fresh startup — catch cases where
    # the task already transitioned to a terminal state but the lock wasn't released
    # (e.g. worker killed by SIGTERM before the finally block ran).
    try:
        import redis.asyncio as _aioredis
        _r = _aioredis.from_url(settings.redis_url)
        cursor = 0
        while True:
            cursor, keys = await _r.scan(cursor, match="openclow:project_lock:*", count=100)
            for key in keys:
                holder = await _r.get(key)
                if holder:
                    holder_id = holder.decode()
                    # Check if the holding task is still active
                    try:
                        import uuid
                        task_uuid = uuid.UUID(holder_id)
                        async with async_session() as session:
                            task_row = await session.execute(
                                sa_select2(Task.status).where(Task.id == task_uuid)
                            )
                            status = task_row.scalar_one_or_none()
                            if status in (None, "failed", "cancelled", "merged",
                                          "diff_preview", "awaiting_approval", "orphaned"):
                                await _r.delete(key)
                                log.info("worker.stale_lock_cleaned", key=key.decode(), holder=holder_id, status=status)
                    except (ValueError, Exception):
                        pass
            if cursor == 0:
                break
        await _r.aclose()
    except Exception as e:
        log.warning("worker.lock_cleanup_failed", error=str(e))

    # Pre-warm whisper model (downloads ~75MB on first use, then cached)
    asyncio.create_task(_prewarm_whisper())

    # Start periodic tunnel health monitor
    asyncio.create_task(_tunnel_health_loop())

    # Start periodic self-maintenance (cleanup orphans, stale workspaces, prune Docker)
    from openclow.worker.tasks.maintenance import maintenance_loop
    asyncio.create_task(maintenance_loop())


async def _start_infra_tunnels():
    """Start dozzle + settings tunnels in background. Never blocks worker startup."""
    # Increase UDP buffer for cloudflared QUIC stability.
    # Without this, tunnels get a URL but the QUIC connection drops immediately.
    try:
        _buf = await asyncio.create_subprocess_exec(
            "sysctl", "-w", "net.core.rmem_max=7500000", "net.core.wmem_max=7500000",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await _buf.communicate()
    except Exception:
        pass  # Non-fatal — tunnels work without it, just less stable

    from openclow.services.tunnel_service import start_tunnel
    try:
        url = await start_tunnel("dozzle", "http://dozzle:8080")
        if url:
            log.info("worker.dashboard_ready", url=url)
        await asyncio.sleep(5)
        settings_url = await start_tunnel("settings", "http://api:8000")
        if settings_url:
            log.info("worker.settings_ready", url=settings_url)
    except Exception as e:
        log.warning("worker.infra_tunnels_failed", error=str(e))



async def _restore_project_tunnels():
    """Restore tunnels for all active projects with running containers.

    On worker restart, project tunnels die (only infra tunnels auto-restart).
    This scans all active projects and restarts their tunnels immediately.
    """
    from openclow.services.tunnel_service import start_tunnel
    from openclow.services.docker_guard import run_docker

    # Wait for infra tunnels to start first (they have priority)
    await asyncio.sleep(10)

    try:
        from sqlalchemy import select as _sa_select
        from openclow.models import Project, async_session

        async with async_session() as session:
            result = await session.execute(
                _sa_select(Project).where(
                    Project.app_port.isnot(None),
                    Project.status == "active",
                )
            )
            projects = result.scalars().all()

        if not projects:
            return

        for p in projects:
            try:
                container = f"openclow-{p.name}-{p.app_container_name or 'app'}-1"

                # Check if container is actually running
                rc, out = await run_docker(
                    "docker", "inspect", "--format",
                    "{{.State.Status}}",
                    container, actor="startup",
                )
                if rc != 0 or out.strip() != "running":
                    continue

                # Get container IP for tunnel target
                rc2, ip_out = await run_docker(
                    "docker", "inspect", "--format",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                    container, actor="startup",
                )
                ip = ip_out.strip() if rc2 == 0 else ""
                # Use internal container port (80), not app_port (which is the
                # host-mapped port). Tunnel connects via Docker network to the
                # container IP, so it needs the port the app actually listens on.
                target = f"http://{ip}:80" if ip else "http://localhost:80"

                url = await start_tunnel(p.name, target)
                if url:
                    log.info("worker.tunnel_restored", project=p.name, url=url)
            except Exception as e:
                log.warning("worker.tunnel_restore_failed", project=p.name, error=str(e))
    except Exception as e:
        log.warning("worker.tunnel_restore_all_failed", error=str(e))


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
    """Check tunnel health every 2 minutes, restart if dead."""
    from openclow.worker.tasks.tunnel_tasks import check_tunnel_health_task

    while True:
        await asyncio.sleep(120)  # 2 minutes — fast recovery for dead tunnels
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


def _load_cron_jobs():
    """T049: register the inactivity reaper on a 5-min cadence.

    ARQ's cron() schedules by clock-wall minute; passing a set via
    `minute={0,5,10,...,55}` gives us one tick every 5 minutes
    regardless of how many workers are running (arq's `unique=True`
    default ensures only one worker executes a given tick).
    """
    from openclow.services.inactivity_reaper import reaper_cron
    return [
        cron(
            reaper_cron,
            name="inactivity_reaper",
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            second=0,
            run_at_startup=False,
            unique=True,
        ),
    ]


class WorkerSettings:
    """arq worker settings."""
    functions = _load_functions()
    cron_jobs = _load_cron_jobs()
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = parse_redis_url(settings.redis_url)
    max_jobs = 2
    job_timeout = 3600  # 1 hour — bootstrap with fresh Docker build takes ~25 min
    max_tries = 1  # stateful tasks (bootstrap, onboarding) must not auto-retry
    health_check_interval = 60
    allow_abort_jobs = True


async def get_arq_pool():
    return await create_pool(parse_redis_url(settings.redis_url))
