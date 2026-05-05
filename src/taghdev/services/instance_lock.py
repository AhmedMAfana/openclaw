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

from taghdev.settings import settings
from taghdev.utils.logging import get_logger

log = get_logger()

_PREFIX = "taghdev:instance"

# Short TTL + heartbeat-while-alive pattern (see InstanceLock._heartbeat).
# Long TTLs were a footgun: a previous design used 3900s (65min) so a
# single long agent turn couldn't self-expire — but if the process died
# mid-run (uvicorn graceful-shutdown timeout, OOM kill, deploy restart),
# the lock stayed for 65min and locked the user out of their own chat
# with "Chat busy — finish previous step before sending more". The
# user's only recourse was an admin redis DEL.
#
# New design: 120s TTL + heartbeat that extends every 60s WHILE the
# holding task is alive. Normal long agent turns stay locked because
# heartbeat keeps the TTL fresh. Process death → no heartbeat → lock
# auto-recovers in <120s. No operator intervention needed.
_DEFAULT_TTL = 120
_HEARTBEAT_INTERVAL_S = 60

# Default patience window when a second request finds the slug busy.
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
        self._heartbeat_task: asyncio.Task | None = None

    async def release(self) -> None:
        """Release the lock if we still own it."""
        # Stop the heartbeat first so it doesn't try to extend a key
        # we're about to delete (or worse, recreate it after we DELETE).
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None
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

    async def _heartbeat_loop(self) -> None:
        """Background task: refresh the lock's TTL every
        ``_HEARTBEAT_INTERVAL_S`` seconds while we hold it.

        This is what makes the short ``_DEFAULT_TTL`` safe for long
        agent turns: the lock stays alive as long as our task is alive
        to refresh it. If the worker / api process dies mid-turn, no
        more heartbeats fire, and the lock self-expires in <TTL — so
        the user isn't stuck behind a dead holder.
        """
        try:
            while self._held:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
                try:
                    current = await self.redis.get(self.key)
                    if current and current.decode() == self.holder_id:
                        # Reset the TTL to its full value, NOT extend by
                        # +N. A fresh full TTL means the next process-death
                        # window is bounded by TTL, not TTL × number of
                        # heartbeats survived.
                        await self.redis.expire(self.key, self.ttl)
                    else:
                        # Lost ownership somehow (TTL expired during a stall,
                        # someone force-released, etc). Stop heartbeating.
                        self._held = False
                        return
                except Exception as e:
                    log.warning(
                        "instance_lock.heartbeat_failed",
                        slug=self.slug, error=str(e),
                    )
        except asyncio.CancelledError:
            # Normal path: release() cancels us.
            pass


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
            lock._heartbeat_task = asyncio.create_task(lock._heartbeat_loop())
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
                    lock._heartbeat_task = asyncio.create_task(lock._heartbeat_loop())
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
