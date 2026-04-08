"""Project-level mutex — prevents concurrent tasks on the same repo.

Two agents working on the same repo simultaneously will:
- Create conflicting git branches
- Step on each other's file edits
- Corrupt Docker state

This provides a Redis-based distributed lock per project.

Usage:
    from openclow.services.project_lock import acquire_project_lock

    lock = await acquire_project_lock(project_id, task_id="abc123", timeout=600)
    if lock is None:
        # Another task is running on this project
        return "Project is busy"
    try:
        ... do work ...
    finally:
        await lock.release()

Or as async context manager:
    async with project_lock(project_id, task_id="abc123") as acquired:
        if not acquired:
            return "Project is busy"
        ... do work ...
"""
import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis.asyncio as aioredis

from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()

# Lock key prefix
_PREFIX = "openclow:project_lock"

# How long a lock lives before auto-expiring (safety net for crashed workers)
_DEFAULT_TTL = 900  # 15 minutes

# How long to wait when trying to acquire a busy lock
_DEFAULT_WAIT = 5  # seconds


class ProjectLock:
    """A Redis-based lock for a specific project."""

    def __init__(self, redis: aioredis.Redis, project_id: int, task_id: str, ttl: int):
        self.redis = redis
        self.key = f"{_PREFIX}:{project_id}"
        self.task_id = task_id
        self.ttl = ttl
        self._held = False

    async def release(self):
        """Release the lock, but only if we still own it."""
        if not self._held:
            return
        try:
            # Atomic check-and-delete: only release if we still hold it
            current = await self.redis.get(self.key)
            if current and current.decode() == self.task_id:
                await self.redis.delete(self.key)
                log.info("project_lock.released", project_id=self.key, task_id=self.task_id)
            self._held = False
        except Exception as e:
            log.warning("project_lock.release_failed", error=str(e))

    async def extend(self, extra_seconds: int = 300):
        """Extend the lock TTL (for long-running tasks)."""
        try:
            current = await self.redis.get(self.key)
            if current and current.decode() == self.task_id:
                await self.redis.expire(self.key, self.ttl + extra_seconds)
                log.debug("project_lock.extended", project_id=self.key, extra=extra_seconds)
        except Exception as e:
            log.warning("project_lock.extend_failed", error=str(e))


async def _get_redis() -> aioredis.Redis:
    """Get a Redis connection."""
    return aioredis.from_url(settings.redis_url, decode_responses=False)


async def acquire_project_lock(
    project_id: int,
    task_id: str = "",
    ttl: int = _DEFAULT_TTL,
    wait: float = _DEFAULT_WAIT,
) -> ProjectLock | None:
    """Try to acquire a project lock.

    Args:
        project_id: The project to lock
        task_id: Identifier for who holds the lock (for debugging)
        ttl: Lock auto-expires after this many seconds
        wait: How long to wait if lock is held (0 = don't wait)

    Returns:
        ProjectLock if acquired, None if project is busy
    """
    r = await _get_redis()
    try:
        lock = ProjectLock(r, project_id, task_id or f"unknown-{time.time()}", ttl)

        # Try to acquire with SET NX (atomic)
        acquired = await r.set(lock.key, lock.task_id, nx=True, ex=ttl)

        if acquired:
            lock._held = True
            log.info("project_lock.acquired", project_id=project_id, task_id=task_id)
            return lock

        # Lock is held — optionally wait
        if wait > 0:
            deadline = time.time() + wait
            while time.time() < deadline:
                await asyncio.sleep(1)
                acquired = await r.set(lock.key, lock.task_id, nx=True, ex=ttl)
                if acquired:
                    lock._held = True
                    log.info("project_lock.acquired_after_wait", project_id=project_id)
                    return lock

        # Still busy
        holder = await r.get(lock.key)
        holder_id = holder.decode() if holder else "unknown"
        log.info("project_lock.busy", project_id=project_id,
                 holder=holder_id, requester=task_id)
        await r.aclose()
        return None
    except Exception:
        await r.aclose()
        raise


async def get_lock_holder(project_id: int) -> str | None:
    """Check who holds the lock (if any). Returns task_id or None."""
    r = await _get_redis()
    try:
        val = await r.get(f"{_PREFIX}:{project_id}")
        return val.decode() if val else None
    finally:
        await r.aclose()


async def force_release(project_id: int):
    """Force release a stuck lock. Use with caution."""
    r = await _get_redis()
    try:
        await r.delete(f"{_PREFIX}:{project_id}")
        log.warning("project_lock.force_released", project_id=project_id)
    finally:
        await r.aclose()


@asynccontextmanager
async def project_lock(
    project_id: int,
    task_id: str = "",
    ttl: int = _DEFAULT_TTL,
    wait: float = _DEFAULT_WAIT,
) -> AsyncIterator[bool]:
    """Async context manager for project lock.

    Usage:
        async with project_lock(project.id, task_id="abc") as acquired:
            if not acquired:
                await chat.send_message(chat_id, "Project busy, try later")
                return
            ... do work ...
    """
    lock = await acquire_project_lock(project_id, task_id, ttl, wait)
    try:
        yield lock is not None
    finally:
        if lock:
            await lock.release()
