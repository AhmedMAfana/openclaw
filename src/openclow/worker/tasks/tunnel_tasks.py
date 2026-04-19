"""Tunnel management tasks — run on worker, called from bot/MCP via arq."""

from openclow.services import tunnel_service
from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()

# Default tunnel definitions (service_name -> target_url)
TUNNEL_DEFAULTS = {
    "dozzle": "http://dozzle:8080",
    "settings": "http://api:8000",
}


async def refresh_dashboard_tunnel(ctx: dict, service_name: str = "dozzle") -> dict:
    """Kill and restart a tunnel. Returns {"url": "...", "ok": True} or {"ok": False, "error": "..."}."""
    target = TUNNEL_DEFAULTS.get(service_name)
    if not target:
        return {"ok": False, "error": f"Unknown service: {service_name}"}

    url = await tunnel_service.refresh_tunnel(service_name, target)
    if url:
        return {"ok": True, "url": url}
    return {"ok": False, "error": "Failed to start tunnel"}


async def stop_dashboard_tunnel(ctx: dict, service_name: str = "dozzle") -> dict:
    """Stop a tunnel."""
    await tunnel_service.stop_tunnel(service_name)
    return {"ok": True}


async def check_tunnel_health_task(ctx: dict) -> dict:
    """Check all registered tunnels + project tunnels, restart dead ones."""
    results = {}

    # Infrastructure tunnels (dozzle, settings)
    for service_name, target_url in TUNNEL_DEFAULTS.items():
        url = await tunnel_service.ensure_tunnel(service_name, target_url)
        results[service_name] = {"url": url, "healthy": url is not None}

    # Project tunnels — restore any that died
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
            # Always ensure a tunnel exists for active projects
            existing_url = await tunnel_service.get_tunnel_url(p.name)
            alive = await tunnel_service.check_tunnel_health(p.name) if existing_url else False

            if alive:
                results[p.name] = {"url": existing_url, "healthy": True}
                continue

            # Race-condition guard: skip if tunnel was just started (< 120s ago).
            # Prevents the health loop from racing with a concurrent repair/start
            # that already launched a fresh cloudflared — the health guard agent
            # can take 1-3 minutes and cloudflared needs time to register with CF.
            if existing_url:
                import time as _time
                cfg = await tunnel_service._get_tunnel_config(p.name)
                started_at = cfg.get("started_at", 0) if cfg else 0
                if _time.time() - started_at < 120:
                    results[p.name] = {"url": existing_url, "healthy": True}
                    log.debug("tunnel.health_loop_skip_fresh", service=p.name,
                              age_s=int(_time.time() - started_at))
                    continue

            # Skip projects with active tasks — the orchestrator/bootstrap owns
            # the tunnel lifecycle during task execution. Don't race with it.
            try:
                from sqlalchemy import select as _sa_select
                from openclow.models import Task, async_session as _async_session
                async with _async_session() as _sess:
                    _active = await _sess.execute(
                        _sa_select(Task.id).where(
                            Task.project_id == p.id,
                            Task.status.in_(("coding", "preparing", "planning",
                                             "reviewing", "pushing")),
                        ).limit(1)
                    )
                    if _active.first() is not None:
                        results[p.name] = {"url": existing_url, "healthy": True}
                        log.debug("tunnel.health_loop_skip_active_task", service=p.name)
                        continue
            except Exception:
                pass  # If DB check fails, proceed with normal health check

            # Also skip if project is bootstrapping
            if p.status == "bootstrapping":
                results[p.name] = {"url": existing_url, "healthy": True}
                log.debug("tunnel.health_loop_skip_bootstrapping", service=p.name)
                continue

            # Tunnel dead or missing — check if containers are actually running first
            compose_project = f"openclow-{p.name}"
            try:
                from openclow.worker.tasks.bootstrap import _get_tunnel_target
                target = await _get_tunnel_target(
                    compose_project, f"{settings.workspace_base_path}/_cache/{p.name}", p.id)
            except Exception:
                target = None

            if not target:
                # No running containers → no point starting tunnel
                results[p.name] = {"url": None, "healthy": False}
                continue

            # Detect host header from .env APP_URL
            import os
            host_header = None
            env_path = f"{settings.workspace_base_path}/_cache/{p.name}/.env"
            if os.path.exists(env_path):
                with open(env_path) as f:
                    for line in f:
                        if line.strip().startswith("APP_URL="):
                            from urllib.parse import urlparse
                            app_url = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                            parsed = urlparse(app_url)
                            if parsed.hostname and parsed.hostname not in ("localhost", "127.0.0.1"):
                                # Only use host_header if it's a custom domain (not an old tunnel URL)
                                if ".trycloudflare.com" not in parsed.hostname:
                                    host_header = parsed.hostname
                            break

            # start_tunnel auto-syncs the container (.env, trustedproxy, caches)
            url = await tunnel_service.start_tunnel(p.name, target, host_header=host_header)
            results[p.name] = {"url": url, "healthy": url is not None}
    except Exception as e:
        log.warning("tunnel.project_health_check_failed", error=str(e))

    return results
