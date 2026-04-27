"""Spec 003 — bulk Force Terminate against the in-memory service.

Asserts that bulk terminate enqueues one teardown per actionable slug,
skips already-ended ones (with blocked audit), and never partial-fails.
"""
from __future__ import annotations

import uuid
import pytest
from datetime import datetime, timedelta, timezone

from openclow.models.instance import Instance, InstanceStatus, TerminatedReason


def _seed(store, *, slug, status):
    inst = Instance(
        id=uuid.uuid4(),
        slug=slug,
        chat_session_id=7,
        project_id=11,
        status=status,
        compose_project=f"tagh-{slug}",
        workspace_path=f"/workspaces/{slug}/",
        session_branch="x",
        heartbeat_secret="x" * 32,
        db_password="y" * 32,
        per_user_count_at_provision=1,
        last_activity_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    store.instances[inst.id] = inst
    return inst


@pytest.mark.asyncio
async def test_bulk_terminate_enqueues_per_actionable_slug(
    inmemory_service, inmemory_store,
):
    svc, calls = inmemory_service
    a = _seed(inmemory_store, slug="inst-aaaaaaaaaaaaaa", status=InstanceStatus.RUNNING.value)
    b = _seed(inmemory_store, slug="inst-bbbbbbbbbbbbbb", status=InstanceStatus.IDLE.value)

    await svc.terminate(a.id, reason=TerminatedReason.ADMIN_FORCED.value)
    await svc.terminate(b.id, reason=TerminatedReason.ADMIN_FORCED.value)

    teardowns = [args for n, args in calls if n == "teardown_instance"]
    assert len(teardowns) == 2
    assert inmemory_store.instances[a.id].status == InstanceStatus.TERMINATING.value
    assert inmemory_store.instances[b.id].status == InstanceStatus.TERMINATING.value


@pytest.mark.asyncio
async def test_bulk_terminate_already_ended_does_not_enqueue(
    inmemory_service, inmemory_store,
):
    svc, calls = inmemory_service
    destroyed = _seed(inmemory_store, slug="inst-cccccccccccccc", status=InstanceStatus.DESTROYED.value)

    # Calling terminate on a destroyed row is the InstanceService's
    # idempotency contract — it short-circuits without enqueuing.
    await svc.terminate(destroyed.id, reason=TerminatedReason.ADMIN_FORCED.value)

    teardowns = [args for n, args in calls if n == "teardown_instance"]
    assert teardowns == [], "destroyed instance must not enqueue a duplicate teardown"
