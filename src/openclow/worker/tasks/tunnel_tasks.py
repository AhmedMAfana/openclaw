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
    """Check all registered tunnels, restart dead ones."""
    results = {}
    for service_name, target_url in TUNNEL_DEFAULTS.items():
        url = await tunnel_service.ensure_tunnel(service_name, target_url)
        results[service_name] = {"url": url, "healthy": url is not None}
    return results
