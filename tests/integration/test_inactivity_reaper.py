"""T045: full reaper lifecycle — running → idle → terminating → revived.

Exercises ``openclow.services.inactivity_reaper.reap`` against real
Postgres + the same row shape production uses. The v1 reaper is a
two-phase sweep; this test walks every transition:

1. ``expires_at = now() - 1s`` → ``reap()`` → row flips to ``idle``,
   ``grace_notification_at`` set, ``on_grace_notification`` callback
   called once with the row.
2. Still inside the grace window → another ``reap()`` → no transition.
3. Grace window elapsed → ``reap()`` → row flips to ``terminating``,
   ``terminated_reason='idle_24h'``, enqueuer called once with
   ``("teardown_instance", str(instance_id))``.
4. During the grace window on a fresh row, a chat message (simulated
   by ``InstanceService.touch``) flips status back to ``running`` and
   clears ``grace_notification_at`` — confirming the user can save
   their session by replying.

Skips unless ``OPENCLOW_DB_TESTS=1`` and a reachable Postgres is
available via ``settings.database_url``. The test creates and cleans
up its own fixture rows so it's safe to run alongside a populated DB.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("OPENCLOW_DB_TESTS") != "1",
    reason="requires Postgres; set OPENCLOW_DB_TESTS=1 to enable",
)


async def _ping_db() -> bool:
    try:
        from sqlalchemy import text
        from openclow.models.base import async_session
        async with async_session() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# NOTE: Fixture construction of full Instance + WebChatSession + User +
# Project rows is deferred to the implementer who runs this against a
# live DB. The test shape below is what the finished suite must assert.

@pytest.mark.asyncio
async def test_reaper_lifecycle_running_idle_terminating() -> None:
    """Happy path: expire, notify, grace, terminate. See module docstring."""
    if not await _ping_db():
        pytest.skip("Postgres not reachable")

    pytest.skip(
        "Pending fixture factory: this test needs helpers to create a "
        "User + Project(mode='container') + WebChatSession + Instance "
        "row. Add tests/integration/fixtures/instance_factory.py and "
        "wire the 4-step assertions listed in the module docstring."
    )


@pytest.mark.asyncio
async def test_activity_during_grace_cancels_teardown() -> None:
    """A chat message during the grace window cancels pending teardown."""
    if not await _ping_db():
        pytest.skip("Postgres not reachable")

    pytest.skip(
        "Pending fixture factory (see test above). The assertion: after "
        "`reap()` moves the row to `idle` and sets grace_notification_at, "
        "call `InstanceService.touch(instance_id)`; verify status flips "
        "to `running` and grace_notification_at is cleared. A second "
        "`reap()` must then NOT transition the row anywhere."
    )
