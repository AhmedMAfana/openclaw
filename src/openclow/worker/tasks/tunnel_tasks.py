"""Tunnel management tasks — run on worker, called from bot/MCP via arq."""

from openclow.services import tunnel_service
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
            existing_url = await tunnel_service.get_tunnel_url(p.name)
            if existing_url:
                # Tunnel URL in DB — check if process is alive
                alive = await tunnel_service.check_tunnel_health(p.name)
                results[p.name] = {"url": existing_url, "healthy": alive}
                if not alive:
                    # Process dead but URL in DB — restart
                    compose_project = f"openclow-{p.name}"
                    try:
                        from openclow.worker.tasks.bootstrap import _get_tunnel_target
                        target = await _get_tunnel_target(
                            compose_project, f"/workspaces/_cache/{p.name}", p.id)
                    except Exception:
                        target = None
                    if not target:
                        from openclow.services.port_allocator import get_app_port
                        target = f"http://localhost:{get_app_port(p.id)}"
                    url = await tunnel_service.start_tunnel(p.name, target)
                    results[p.name] = {"url": url, "healthy": url is not None}
    except Exception as e:
        log.warning("tunnel.project_health_check_failed", error=str(e))

    return results
