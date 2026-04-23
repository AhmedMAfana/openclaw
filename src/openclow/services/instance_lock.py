"""Per-instance mutex — serialises agent work on one chat's instance.

Spec: specs/001-per-chat-instances/tasks.md T037a; FR-028 (no two tasks
run concurrently against the same instance).

Two tasks against the same instance cannot overlap, because they would:
  * Step on each other's file edits in the instance workspace.
  * Interleave ``docker compose exec`` output on the same container.
  * Produce two conflicting commits on the same session branch.

This module mirrors ``project_lock.py`` one-for-one, re-scoped to the
instance slug. Where a host-mode project serialises at the project, a
container-mode chat serialises at its instance — both use the same
Redis SET-NX + TTL primitive.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis.asyncio as aioredis

from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()

_PREFIX = "openclow:instance"

# Must be >= arq job_timeout (3600s) so a single long task cannot expire
# its own lock mid-run. Matches the project-lock safety margin.
_DEFAULT_TTL = 3900

# Default patience window when a second request finds the slug busy.
# Chosen to absorb normal turn-taking latency without letting a
# broken/hung task block a user for more than a moment.
_DEFAULT_WAIT = 30


class InstanceLock:
    """A Redis SET-NX lock keyed by instance slug. Mirrors ``ProjectLock``."""

    def __init__(
        self,
        redis: aioredis.Redis,
        slug: str,
        holder_id: str,
        ttl: int,
    ) -> None:
        self.redis = redis
        self.key = f"{_PREFIX}:{slug}"
        self.slug = slug
        self.holder_id = holder_id
        self.ttl = ttl
        self._held = False

    async def release(self) -> None:
        """Release the lock if we still own it."""
        if not self._held:
            return
        try:
            current = await self.redis.get(self.key)
            if current and current.decode() == self.holder_id:
                await self.redis.delete(self.key)
                log.info(
                    "instance_lock.released",
                    slug=self.slug, holder=self.holder_id,
                )
            self._held = False
        except Exception as e:
            log.warning("instance_lock.release_failed", error=str(e))

    async def extend(self, extra_seconds: int = 300) -> None:
        """Push the TTL out for long-running agent turns."""
        try:
            current = await self.redis.get(self.key)
            if current and current.decode() == self.holder_id:
                await self.redis.expire(self.key, self.ttl + extra_seconds)
        except Exception as e:
            log.warning("instance_lock.extend_failed", error=str(e))


async def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=False)


async def acquire_instance_lock(
    slug: str,
    holder_id: str = "",
    ttl: int = _DEFAULT_TTL,
    wait: float = _DEFAULT_WAIT,
) -> InstanceLock | None:
    """Try to acquire a lock scoped to ``slug``.

    Returns an ``InstanceLock`` when acquired, or ``None`` when the
    slug is still busy after ``wait`` seconds. Callers should treat
    ``None`` as "tell the user the instance is busy" — never silently
    proceed.
    """
    r = await _get_redis()
    try:
        lock = InstanceLock(r, slug, holder_id or f"unknown-{time.time()}", ttl)
        acquired = await r.set(lock.key, lock.holder_id, nx=True, ex=ttl)
        if acquired:
            lock._held = True
            log.info("instance_lock.acquired", slug=slug, holder=holder_id)
            return lock

        if wait > 0:
            deadline = time.time() + wait
            while time.time() < deadline:
                # 500ms poll matches project_lock's 1s with a tighter beat
                # so chats feel snappy even under contention.
                await asyncio.sleep(0.5)
                acquired = await r.set(
                    lock.key, lock.holder_id, nx=True, ex=ttl
                )
                if acquired:
                    lock._held = True
                    log.info(
                        "instance_lock.acquired_after_wait",
                        slug=slug, holder=holder_id,
                    )
                    return lock

        holder = await r.get(lock.key)
        holder_label = holder.decode() if holder else "unknown"
        log.info(
            "instance_lock.busy",
            slug=slug, holder=holder_label, requester=holder_id,
        )
        await r.aclose()
        return None
    except Exception:
        await r.aclose()
        raise


async def get_lock_holder(slug: str) -> str | None:
    """Return the current holder_id of a lock, or None if free."""
    r = await _get_redis()
    try:
        val = await r.get(f"{_PREFIX}:{slug}")
        return val.decode() if val else None
    finally:
        await r.aclose()


async def force_release(slug: str) -> None:
    """Force-release a stuck lock. Operator-only escape hatch."""
    r = await _get_redis()
    try:
        await r.delete(f"{_PREFIX}:{slug}")
        log.warning("instance_lock.force_released", slug=slug)
    finally:
        await r.aclose()


@asynccontextmanager
async def instance_lock(
    slug: str,
    holder_id: str = "",
    ttl: int = _DEFAULT_TTL,
    wait: float = _DEFAULT_WAIT,
) -> AsyncIterator[bool]:
    """Async context manager. Yields True if acquired, False if busy.

    Usage:

        async with instance_lock(instance.slug, holder_id=chat_msg_id) as ok:
            if not ok:
                controller.append_text("This chat is busy finishing a "
                                        "previous step — try again shortly.")
                return
            ... run the agent ...
    """
    lock = await acquire_instance_lock(slug, holder_id, ttl, wait)
    try:
        yield lock is not None
    finally:
        if lock:
            await lock.release()
