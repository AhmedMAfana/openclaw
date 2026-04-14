"""Cloudflare tunnel lifecycle management.

Starts/stops cloudflared quick-tunnels, persists URLs in DB,
monitors health, and auto-restarts dead tunnels.

Usage (from worker):
    from openclow.services.tunnel_service import start_tunnel, get_tunnel_url
    url = await start_tunnel("dozzle", "http://dozzle:8080")

Usage (from bot — fast DB read):
    from openclow.services.tunnel_service import get_tunnel_url
    url = await get_tunnel_url("dozzle")  # instant
"""

import asyncio
import os
import re
import time
import uuid

from openclow.services.config_service import get_config, set_config
from openclow.utils.logging import get_logger

log = get_logger()

TUNNEL_CATEGORY = "tunnel"

# Module-level state: maps service_name -> asyncio.subprocess.Process
_active_processes: dict[str, asyncio.subprocess.Process] = {}

# Unique ID per worker boot — detects stale DB entries after restart
_worker_instance_id: str = uuid.uuid4().hex[:12]

# Rate limit cooldown — when Cloudflare returns 429, stop ALL tunnel attempts
# until this timestamp. Prevents the death spiral of retries making it worse.
_rate_limit_until: list[float] = [0.0]  # mutable list so nested functions can write


async def start_tunnel(
    service_name: str, target_url: str, host_header: str | None = None,
) -> str | None:
    """Start a cloudflared quick-tunnel to target_url.

    Idempotent: if tunnel already running for this service, returns existing URL.
    Persists URL in DB under category="tunnel", key=service_name.

    Args:
        service_name: Unique name for this tunnel (e.g. "tagh-test")
        target_url: URL to proxy to (e.g. "http://172.21.0.7:80")
        host_header: Override Host header sent to origin (e.g. "abc.test").
                     Required for apps that use virtual hosts / server_name matching.

    Returns the public URL or None on failure.
    """
    # Rate limit cooldown — don't even try if Cloudflare is blocking us
    if time.time() < _rate_limit_until[0]:
        remaining = int(_rate_limit_until[0] - time.time())
        log.debug("tunnel.cooldown_active", service=service_name, remaining_s=remaining)
        return None

    # Check if we already have an active process handle
    existing_proc = _active_processes.get(service_name)
    if existing_proc and existing_proc.returncode is None:
        config = await get_config(TUNNEL_CATEGORY, service_name)
        if config and config.get("url"):
            return config["url"]

    # No in-memory handle (e.g. after worker restart) — check if the old
    # cloudflared process from DB is still alive. If so, reuse it instead
    # of killing and creating a new tunnel (avoids Cloudflare rate limits).
    config = await get_config(TUNNEL_CATEGORY, service_name)
    if config and config.get("url") and config.get("pid"):
        old_pid = config["pid"]
        try:
            proc_check = await asyncio.create_subprocess_exec(
                "ps", "-p", str(old_pid), "-o", "comm=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc_check.communicate()
            if "cloudflared" in stdout.decode().strip():
                log.info("tunnel.reusing_existing", service=service_name,
                         pid=old_pid, url=config["url"])
                return config["url"]
        except (OSError, ProcessLookupError):
            pass

    # Kill any stale process first
    await stop_tunnel(service_name)

    # Start cloudflared
    cmd = ["cloudflared", "tunnel", "--url", target_url]
    if host_header:
        cmd.extend(["--http-host-header", host_header])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.error("tunnel.cloudflared_not_found", service=service_name)
        return None

    # Extract URL from stderr (cloudflared prints it within ~5-10s)
    url = None
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            line = await asyncio.wait_for(proc.stderr.readline(), timeout=2)
            text = line.decode()
            match = re.search(r"(https://[a-z0-9-]+\.trycloudflare\.com)", text)
            if match:
                url = match.group(1)
                break
        except asyncio.TimeoutError:
            continue

    if not url:
        # Check if cloudflared already exited (e.g. 429 rate limit)
        if proc.returncode is not None:
            try:
                remaining = await proc.stderr.read(4096)
                stderr_text = remaining.decode()
            except Exception:
                stderr_text = ""
            if "429" in stderr_text or "Too Many Requests" in stderr_text:
                # Set a cooldown — don't attempt ANY tunnels for 10 minutes
                _rate_limit_until[0] = time.time() + 600
                log.warning("tunnel.rate_limited", service=service_name,
                            cooldown_minutes=10,
                            hint="Will auto-retry via health loop after cooldown")
                try:
                    proc.kill()
                except (OSError, ProcessLookupError):
                    pass
                return None
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass
        log.error("tunnel.no_url", service=service_name, target=target_url)
        return None

    # Drain stderr so buffer doesn't fill up and block
    drain_task = asyncio.create_task(_drain_stderr(service_name, proc))
    drain_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    # Wait briefly for QUIC connection to stabilize before verifying.
    # Cloudflared prints the URL before the tunnel is fully registered with CF.
    await asyncio.sleep(2)

    # Quick health check — process still alive?
    verified = proc.returncode is None
    if not verified:
        log.warning("tunnel.died_after_start", service=service_name, url=url)

    # Store in DB and memory
    _active_processes[service_name] = proc
    await set_config(TUNNEL_CATEGORY, service_name, {
        "url": url,
        "target": target_url,
        "pid": proc.pid,
        "started_at": time.time(),
        "worker_id": _worker_instance_id,
    })

    log.info("tunnel.started", service=service_name, url=url, pid=proc.pid)

    # Auto-sync project container (update .env, inject trustedproxy, clear caches)
    # Awaited, not fire-and-forget — callers need the sync done before they use the URL.
    try:
        await sync_project_tunnel(service_name, url)
    except Exception as e:
        log.warning("tunnel.auto_sync_failed", service=service_name, error=str(e))

    return url


# Infrastructure tunnels — never need container sync
_INFRA_TUNNELS = {"dozzle", "settings"}


async def sync_project_tunnel(service_name: str, tunnel_url: str):
    """Sync a project's container after tunnel URL change.

    Updates host .env, container .env, injects config/trustedproxy.php,
    and clears framework caches. Skips infrastructure tunnels.

    Called automatically by start_tunnel() — callers don't need to do this.
    """
    if service_name in _INFRA_TUNNELS:
        return

    import re as _re
    from openclow.settings import settings as app_settings

    # ── Find project in DB ──
    try:
        from openclow.models import Project, async_session
        from sqlalchemy import select
        async with async_session() as session:
            # Don't filter by status — if a tunnel runs, the container needs sync
            result = await session.execute(
                select(Project).where(Project.name == service_name)
            )
            project = result.scalar_one_or_none()
        if not project:
            return
    except Exception as e:
        log.warning("tunnel.sync_project_lookup_failed", service=service_name, error=str(e))
        return

    workspace = f"{app_settings.workspace_base_path}/_cache/{service_name}"
    compose_project = f"openclow-{service_name}"
    container_name = project.app_container_name or "laravel.test"
    container = f"{compose_project}-{container_name}-1"

    # ── 1. Update host .env ──
    env_path = os.path.join(workspace, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path) as f:
                content = f.read()
            # Replace all old trycloudflare URLs
            content = _re.sub(
                r'https://[a-z0-9-]+\.trycloudflare\.com',
                tunnel_url, content,
            )
            # Ensure APP_URL and ASSET_URL point to HTTPS tunnel
            for key in ("APP_URL", "ASSET_URL"):
                if _re.search(rf'^{key}=', content, _re.MULTILINE):
                    content = _re.sub(rf'^{key}=.*$', f'{key}={tunnel_url}', content, flags=_re.MULTILINE)
                else:
                    content += f"\n{key}={tunnel_url}\n"
            with open(env_path, "w") as f:
                f.write(content)
        except Exception as e:
            log.warning("tunnel.sync_host_env_failed", service=service_name, error=str(e))

    # ── 2. Update container .env + inject trustedproxy + clear caches ──
    try:
        from openclow.services.docker_guard import run_docker

        # Replace tunnel URLs in container .env
        for env_loc in (".env", "/var/www/html/.env", "/app/.env"):
            await run_docker(
                "docker", "exec", container, "sh", "-c",
                f"[ -f {env_loc} ] && sed -i 's|https://[a-z0-9-]*\\.trycloudflare\\.com|{tunnel_url}|g' {env_loc} || true",
                actor="tunnel_sync", timeout=10,
            )
            # Set ASSET_URL to full HTTPS tunnel URL
            await run_docker(
                "docker", "exec", container, "sh", "-c",
                f"[ -f {env_loc} ] && (grep -q '^ASSET_URL=' {env_loc} && sed -i 's|^ASSET_URL=.*|ASSET_URL={tunnel_url}|' {env_loc} || echo 'ASSET_URL={tunnel_url}' >> {env_loc}) || true",
                actor="tunnel_sync", timeout=10,
            )

        # Inject config/trustedproxy.php — Laravel needs this to trust
        # cloudflared's X-Forwarded-Proto header. Without it → mixed content.
        # Use base64 to avoid shell quoting issues with PHP single quotes.
        import base64
        trust_php = b"<?php return ['proxies' => '*', 'headers' => -1];"
        b64 = base64.b64encode(trust_php).decode()
        await run_docker(
            "docker", "exec", container, "sh", "-c",
            f"for d in . /var/www/html /app; do [ -d \"$d/config\" ] && echo {b64} | base64 -d > \"$d/config/trustedproxy.php\" && break; done || true",
            actor="tunnel_sync", timeout=10,
        )

        # Clear all framework caches
        await run_docker(
            "docker", "exec", container, "sh", "-c",
            "php artisan config:clear 2>/dev/null; php artisan cache:clear 2>/dev/null; "
            "php artisan view:clear 2>/dev/null; php artisan route:clear 2>/dev/null || true",
            actor="tunnel_sync", timeout=10,
        )

        log.info("tunnel.project_synced", service=service_name, url=tunnel_url)
    except Exception as e:
        log.warning("tunnel.sync_container_failed", service=service_name, error=str(e))


async def stop_tunnel(service_name: str) -> None:
    """Stop a tunnel by service name. Cleans up process and DB entry."""
    # Kill in-memory process handle
    proc = _active_processes.pop(service_name, None)
    if proc and proc.returncode is None:
        try:
            proc.kill()
            await proc.wait()
        except (OSError, ProcessLookupError):
            pass

    # Also try PID from DB (in case process handle was lost)
    config = await get_config(TUNNEL_CATEGORY, service_name)
    if config and config.get("pid"):
        pid = config["pid"]
        try:
            # Verify the PID is actually a cloudflared process before killing
            proc_check = await asyncio.create_subprocess_exec(
                "ps", "-p", str(pid), "-o", "comm=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc_check.communicate()
            proc_name = stdout.decode().strip()
            if "cloudflared" in proc_name:
                os.kill(pid, 9)
            else:
                log.warning("tunnel.stale_pid", pid=pid, actual_process=proc_name)
        except (OSError, ProcessLookupError):
            pass

    # Clear DB entry
    await set_config(TUNNEL_CATEGORY, service_name, {})
    log.info("tunnel.stopped", service=service_name)


async def get_tunnel_url(service_name: str) -> str | None:
    """Read tunnel URL from DB. This is the FAST path for the bot."""
    config = await get_config(TUNNEL_CATEGORY, service_name)
    if config and config.get("url"):
        return config["url"]
    return None


async def check_tunnel_health(service_name: str) -> bool:
    """Check if a tunnel is alive (process running + URL in DB)."""
    proc = _active_processes.get(service_name)
    if proc and proc.returncode is None:
        config = await get_config(TUNNEL_CATEGORY, service_name)
        return bool(config and config.get("url"))
    return False


async def ensure_tunnel(service_name: str, target_url: str) -> str | None:
    """Ensure a tunnel is running. If dead, restart it.

    Called by the periodic health monitor.
    """
    if await check_tunnel_health(service_name):
        return await get_tunnel_url(service_name)

    log.info("tunnel.restarting", service=service_name, reason="health_check_failed")
    return await start_tunnel(service_name, target_url)


async def refresh_tunnel(service_name: str, target_url: str) -> str | None:
    """Kill old tunnel, start fresh one. Used by 'Refresh' button."""
    await stop_tunnel(service_name)
    return await start_tunnel(service_name, target_url)


async def _drain_stderr(service_name: str, proc: asyncio.subprocess.Process):
    """Drain stderr to prevent buffer deadlock. Log errors."""
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode().strip()
            if "error" in text.lower() or "failed" in text.lower():
                log.warning("tunnel.stderr", service=service_name, line=text)
    except Exception:
        pass
    finally:
        log.warning("tunnel.process_exited", service=service_name,
                    returncode=proc.returncode)
        _active_processes.pop(service_name, None)
