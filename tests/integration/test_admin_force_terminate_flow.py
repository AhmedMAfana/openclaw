"""Spec 003 — Force Terminate flow against the in-memory InstanceService.

Asserts that ``InstanceService.terminate(reason='admin_forced')``:
* transitions a running instance to ``terminating``,
* enqueues exactly one ``teardown_instance`` job,
* records ``terminated_reason='admin_forced'``.

The HTTP layer is exercised by ``tests/contract/test_admin_instances_terminate.py``;
this test pins the service-level contract that the admin endpoint depends on.
"""
from __future__ import annotations

import uuid
import pytest
from datetime import datetime, timedelta, timezone

from openclow.models.instance import Instance, InstanceStatus, TerminatedReason


def _seed_running_instance(store, *, slug: str = "inst-aaaaaaaaaaaaaa", chat_id: int = 7):
    inst = Instance(
        id=uuid.uuid4(),
        slug=slug,
        chat_session_id=chat_id,
        project_id=11,
        status=InstanceStatus.RUNNING.value,
        compose_project=f"tagh-{slug}",
        workspace_path=f"/workspaces/{slug}/",
        session_branch="chat/7-main",
        heartbeat_secret="x" * 32,
        db_password="y" * 32,
        per_user_count_at_provision=1,
        last_activity_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    store.instances[inst.id] = inst
    return inst


@pytest.mark.asyncio
async def test_admin_forced_terminate_marks_terminating_and_enqueues_teardown(
    inmemory_service, inmemory_store,
):
    svc, calls = inmemory_service
    inst = _seed_running_instance(inmemory_store)

    await svc.terminate(inst.id, reason=TerminatedReason.ADMIN_FORCED.value)

    stored = inmemory_store.instances[inst.id]
    assert stored.status == InstanceStatus.TERMINATING.value
    assert stored.terminated_reason == TerminatedReason.ADMIN_FORCED.value
    assert any(name == "teardown_instance" for name, _ in calls)
    assert sum(1 for n, _ in calls if n == "teardown_instance") == 1


@pytest.mark.asyncio
async def test_double_terminate_is_idempotent_no_duplicate_teardown(
    inmemory_service, inmemory_store,
):
    svc, calls = inmemory_service
    inst = _seed_running_instance(inmemory_store)

    await svc.terminate(inst.id, reason=TerminatedReason.ADMIN_FORCED.value)
    await svc.terminate(inst.id, reason=TerminatedReason.ADMIN_FORCED.value)

    teardown_calls = [args for name, args in calls if name == "teardown_instance"]
    assert len(teardown_calls) == 1, (
        f"expected 1 teardown enqueue, got {len(teardown_calls)} — Principle VI violation"
    )


@pytest.mark.asyncio
async def test_terminate_unknown_reason_is_rejected(
    inmemory_service, inmemory_store,
):
    svc, _ = inmemory_service
    inst = _seed_running_instance(inmemory_store)
    with pytest.raises(ValueError):
        await svc.terminate(inst.id, reason="bogus_reason")
