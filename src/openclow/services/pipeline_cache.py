"""Pipeline cache — skip redundant health checks for subsequent tasks.

When a task verifies project health, the result is cached in Redis.
Subsequent tasks in the same session reuse the cached result instead of
re-running the full health check (which can take 1-120s with LLM repair).

Cache is invalidated on task failure so the next task gets a fresh check.
"""
from __future__ import annotations

import json

import redis.asyncio as aioredis

from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()

_PREFIX = "openclow:health_cache"


async def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def get_cached_health(project_id: int) -> tuple[bool, str | None] | None:
    """Return (healthy, tunnel_url) if cache hit, else None."""
    r = await _get_redis()
    try:
        raw = await r.get(f"{_PREFIX}:{project_id}")
        if raw is None:
            return None
        data = json.loads(raw)
        return data["healthy"], data.get("tunnel_url")
    except Exception as e:
        log.warning("pipeline_cache.get_failed", project_id=project_id, error=str(e))
        return None
    finally:
        await r.aclose()


async def set_health_cache(
    project_id: int, healthy: bool, tunnel_url: str | None,
) -> None:
    """Store health result with TTL."""
    r = await _get_redis()
    try:
        data = json.dumps({"healthy": healthy, "tunnel_url": tunnel_url})
        await r.set(f"{_PREFIX}:{project_id}", data, ex=settings.health_cache_ttl)
        log.info("pipeline_cache.set", project_id=project_id, healthy=healthy)
    except Exception as e:
        log.warning("pipeline_cache.set_failed", project_id=project_id, error=str(e))
    finally:
        await r.aclose()


async def invalidate_health_cache(project_id: int) -> None:
    """Delete cache entry — next task must re-verify."""
    r = await _get_redis()
    try:
        await r.delete(f"{_PREFIX}:{project_id}")
        log.info("pipeline_cache.invalidated", project_id=project_id)
    except Exception as e:
        log.warning("pipeline_cache.invalidate_failed", project_id=project_id, error=str(e))
    finally:
        await r.aclose()
