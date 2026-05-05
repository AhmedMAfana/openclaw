"""T037a: two concurrent tasks on one chat's instance run serially.

FR-028: agent work on the same instance MUST NOT interleave. This test
exercises the Redis mutex in ``taghdev.services.instance_lock`` by
acquiring the same slug from two coroutines and asserting only one is
allowed inside the critical section at any moment.

The lock itself is the primary unit under test. Wiring in
``assistant.py`` is covered indirectly — the lock is what that wiring
depends on; a leak there would be a call-site bug, not a lock bug.

Skips unless OPENCLOW_REDIS_TESTS=1 and a reachable Redis is available
via ``settings.redis_url`` — CI runs it in the integration job; local
runs without Redis skip cleanly.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("OPENCLOW_REDIS_TESTS") != "1",
    reason="requires Redis; set OPENCLOW_REDIS_TESTS=1 to enable",
)


async def _ping_redis() -> bool:
    try:
        import redis.asyncio as aioredis
        from taghdev.settings import settings
        r = aioredis.from_url(settings.redis_url)
        try:
            await r.ping()
        finally:
            await r.aclose()
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_two_tasks_against_one_instance_run_serially() -> None:
    if not await _ping_redis():
        pytest.skip("Redis not reachable")

    from taghdev.services.instance_lock import (
        instance_lock,
        force_release,
    )

    slug = f"inst-{uuid.uuid4().hex[:14]}"
    try:
        observations: list[tuple[str, float]] = []
        lock_held = asyncio.Event()
        release = asyncio.Event()

        async def task_a() -> None:
            # Acquires first. Records enter, waits for the test to tell
            # it to leave, records exit. The hold window is where
            # serialisation is visible.
            async with instance_lock(slug, holder_id="task-a", wait=0) as ok:
                assert ok, "task A failed to acquire a free lock"
                observations.append(("a_enter", time.monotonic()))
                lock_held.set()
                await release.wait()
                observations.append(("a_exit", time.monotonic()))

        async def task_b() -> None:
            # Starts AFTER task_a has the lock. Must wait until task_a
            # releases before its body runs, even though `wait=5` would
            # normally time out. Patience window > the artificial hold.
            await lock_held.wait()
            async with instance_lock(
                slug, holder_id="task-b", wait=5,
            ) as ok:
                assert ok, "task B should acquire once A releases"
                observations.append(("b_enter", time.monotonic()))
                observations.append(("b_exit", time.monotonic()))

        a = asyncio.create_task(task_a())
        b = asyncio.create_task(task_b())

        # Let B start its wait loop, then release A to let B through.
        await asyncio.sleep(0.2)
        release.set()
        await asyncio.gather(a, b)

        assert [o[0] for o in observations] == [
            "a_enter", "a_exit", "b_enter", "b_exit",
        ], f"serialisation broken: {observations}"
    finally:
        await force_release(slug)


@pytest.mark.asyncio
async def test_second_acquire_without_wait_returns_none() -> None:
    """wait=0 must fail fast when the slug is busy."""
    if not await _ping_redis():
        pytest.skip("Redis not reachable")

    from taghdev.services.instance_lock import (
        acquire_instance_lock, force_release,
    )

    slug = f"inst-{uuid.uuid4().hex[:14]}"
    try:
        first = await acquire_instance_lock(slug, holder_id="first", wait=0)
        assert first is not None
        try:
            second = await acquire_instance_lock(slug, holder_id="second", wait=0)
            assert second is None, "second acquire must return None on contention"
        finally:
            await first.release()
    finally:
        await force_release(slug)
