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
            proj_url = await start_tunnel(p.name, tunnel_target)
            if proj_url:
                log.info("worker.project_tunnel_restored", project=p.name, url=proj_url)
    except Exception as e:
        log.warning("worker.project_tunnel_restore_failed", error=str(e))

    # Pre-warm whisper model (downloads ~75MB on first use, then cached)
    asyncio.create_task(_prewarm_whisper())

    # Start periodic tunnel health monitor
    asyncio.create_task(_tunnel_health_loop())


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
