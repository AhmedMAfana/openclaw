"""Bot lifecycle management — automatic restart on provider change.

The bot runs in a separate container. When the chat provider config changes,
this task restarts the bot container and verifies it comes back healthy.

Container names are discovered dynamically via Docker compose labels —
never hardcoded.
"""
import asyncio

from openclow.services.activity_log import log_event
from openclow.services.docker_guard import run_docker
from openclow.utils.logging import get_logger

log = get_logger()


async def _find_container_by_service(service: str) -> str | None:
    """Discover a container name by its docker-compose service label.

    Returns the container name (e.g. 'openclow-bot-1') or None.
    Uses Docker's label filter — works regardless of project name or replicas.
    """
    rc, output = await run_docker(
        "docker", "ps",
        "--filter", f"label=com.docker.compose.service={service}",
        "--format", "{{.Names}}",
        actor="system", timeout=10,
    )
    if rc != 0 or not output.strip():
        return None
    return output.strip().split("\n")[0]


async def _get_container_health(container: str) -> str:
    """Get the health status of a container. Returns 'healthy', 'unhealthy', 'starting', or 'none'."""
    rc, output = await run_docker(
        "docker", "inspect", "--format",
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
        container,
        actor="system", timeout=10,
    )
    if rc != 0:
        return "unknown"
    return output.strip()


async def _get_container_state(container: str) -> str:
    """Get the running state of a container. Returns 'running', 'exited', etc."""
    rc, output = await run_docker(
        "docker", "inspect", "--format", "{{.State.Status}}",
        container,
        actor="system", timeout=10,
    )
    if rc != 0:
        return "unknown"
    return output.strip()


async def restart_bot_task(ctx: dict, reason: str = "config_change"):
    """Restart the bot container and verify it comes back healthy.

    Called automatically when chat provider config changes in the dashboard.
    Reports progress via activity log (consumed by SSE on the dashboard).
    """
    log.info("bot_lifecycle.restart_starting", reason=reason)
    log_event("bot_restart", {"status": "starting", "reason": reason})

    # 1. Discover the bot container dynamically
    container = await _find_container_by_service("bot")
    if not container:
        error = "Bot container not found — is docker compose running?"
        log.error("bot_lifecycle.container_not_found")
        log_event("bot_restart", {"status": "failed", "error": error})
        return {"ok": False, "error": error}

    log_event("bot_restart", {"status": "restarting", "container": container})

    # 2. Restart the container
    rc, output = await run_docker(
        "docker", "restart", container,
        actor="system", timeout=60,
    )
    if rc != 0:
        error = f"docker restart failed (rc={rc}): {output[:200]}"
        log.error("bot_lifecycle.restart_failed", error=error)
        log_event("bot_restart", {"status": "failed", "error": error})
        return {"ok": False, "error": error}

    log.info("bot_lifecycle.container_restarted", container=container)
    log_event("bot_restart", {"status": "waiting_healthy", "container": container})

    # 3. Wait for healthy — quick poll first, then agent if stuck
    healthy = False
    for attempt in range(10):
        await asyncio.sleep(3)
        health = await _get_container_health(container)
        state = await _get_container_state(container)

        if state != "running":
            break

        if health == "healthy":
            healthy = True
            break

    # If not healthy after 30s, run agentic repair
    if not healthy:
        log.warning("bot_lifecycle.unhealthy_triggering_agent", container=container)
        log_event("bot_restart", {"status": "agent_repair", "container": container})
        try:
            from openclow.worker.tasks._agent_helper import run_repair_agent

            # Get container details for rich context
            logs = ""
            try:
                from openclow.services.docker_guard import run_docker as _run_docker
                _, logs = await _run_docker("docker", "logs", container, "--tail", "80", actor="bot_repair", timeout=10)
            except Exception:
                pass

            prompt = (
                f"Bot container '{container}' was restarted but is NOT healthy. Fix it.\n\n"
                f"CONTAINER: {container}\n"
                f"CURRENT STATE: {await _get_container_state(container)}\n"
                f"HEALTH: {await _get_container_health(container)}\n\n"
                f"RECENT LOGS:\n```\n{logs[-2000:]}\n```\n\n"
                f"STEPS:\n"
                f"1. container_logs(\"{container}\", 80) — read full logs\n"
                f"2. DIAGNOSIS: <analyze what's wrong from logs>\n"
                f"3. ACTION: <fix it — restart_container, docker_exec, edit config>\n"
                f"4. container_health(\"{container}\") — verify healthy\n\n"
                f"OUTPUT: STATUS: <step> | DIAGNOSIS: <issue> | ACTION: <fix> | FIXED: <summary>\n\n"
                f"SELF-HEALING: When a fix doesn't work, try a different approach. Never give up."
            )
            status_lines = []
            fixed = await run_repair_agent(
                prompt, "/app", chat=None, chat_id="", message_id="",
                status_lines=status_lines, max_turns=15,
                notify_fn=lambda text: log_event("bot_restart", {"status": "agent", "detail": text[-200:]}),
            )
            if fixed:
                healthy = True
        except Exception as e:
            log.error("bot_lifecycle.agent_repair_failed", error=str(e))

    if not healthy:
        error = "Bot did not become healthy — agent could not fix"
        log.error("bot_lifecycle.health_failed")
        log_event("bot_restart", {"status": "failed", "error": error})
        return {"ok": False, "error": error}

    # 4. Get the new provider type for reporting
    try:
        from openclow.services.config_service import get_provider_config
        ptype, _ = await get_provider_config("chat")
    except Exception:
        ptype = "unknown"

    log.info("bot_lifecycle.restart_complete", provider=ptype)
    log_event("bot_restart", {"status": "completed", "provider": ptype})

    return {"ok": True, "provider": ptype}


async def get_bot_status() -> dict:
    """Get the current bot container status. Used by the dashboard API."""
    container = await _find_container_by_service("bot")
    if not container:
        return {"running": False, "health": "not_found", "container": None, "provider": None}

    state = await _get_container_state(container)
    health = await _get_container_health(container)

    try:
        from openclow.services.config_service import get_provider_config
        ptype, _ = await get_provider_config("chat")
    except Exception:
        ptype = "unknown"

    return {
        "running": state == "running",
        "health": health,
        "container": container,
        "provider": ptype,
    }


async def get_bot_status_task(ctx: dict) -> dict:
    """arq task wrapper for get_bot_status — called by the API."""
    return await get_bot_status()
