"""GitHub tasks — run on worker which has gh CLI and token access."""
import asyncio
import json
import os

from taghdev.utils.logging import get_logger

log = get_logger()


async def _get_github_token() -> str:
    """Get GitHub token from DB config."""
    from taghdev.services.config_service import get_config
    config = await get_config("git", "provider")
    if config:
        return config.get("token", "")
    return ""


async def list_github_repos(ctx: dict) -> list[dict]:
    """List GitHub repos accessible by the configured token."""
    token = await _get_github_token()
    if not token:
        log.error("github.no_token")
        return []

    env = {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token}

    proc = await asyncio.create_subprocess_exec(
        "gh", "repo", "list", "--limit", "20",
        "--json", "nameWithOwner,description",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        log.error("github.list_timeout")
        return []

    if proc.returncode != 0:
        log.error("github.list_failed", stderr=stderr.decode()[:200])
        return []

    try:
        repos = json.loads(stdout.decode())
        return [
            {"name": r.get("nameWithOwner", ""), "desc": r.get("description", "") or ""}
            for r in repos
        ]
    except json.JSONDecodeError:
        log.error("github.parse_failed")
        return []
