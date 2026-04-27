"""T034: per-user concurrency cap (FR-030a) integration test.

Four properties, all driven through the fixture factory:

  1. Three chats for one user each provision — all three succeed.
  2. A fourth chat raises ``PerUserCapExceeded`` with
     ``active_chat_ids`` populated with the prior three chat IDs
     (FR-030b navigation surface).
  3. Terminating one of the three frees a slot: the fourth chat
     provisions successfully on retry.
  4. Raising the cap via ``platform_config`` (category='instance',
     key='per_user_cap') takes effect on the NEXT provision call
     without restarting the worker (T053 / FR-030a).

Guarded by ``OPENCLOW_DB_TESTS=1``.
"""
from __future__ import annotations

import os

import pytest

from openclow.services.instance_service import (
    InstanceService,
    PerUserCapExceeded,
)

from tests.integration.fixtures.instance_factory import (
    instance_fixture,
    platform_config_override,
)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("OPENCLOW_DB_TESTS"),
        reason="per-user cap needs OPENCLOW_DB_TESTS=1 + live Postgres + Redis",
    ),
]


async def _new_chat_for_user(user_id: int, project_id: int):
    """Create an extra WebChatSession for an existing user + project.

    Returns the new chat's id. The outer fixture owns teardown of the
    user / project / any instances it creates; this helper just adds
    another chat row so we can exercise the per-user cap across
    multiple chats.
    """
    from datetime import datetime, timezone
    from openclow.models import WebChatSession, async_session
    async with async_session() as s:
        chat = WebChatSession(
            user_id=user_id,
            project_id=project_id,
            title=f"cap-test-{datetime.now(timezone.utc).isoformat()}",
        )
        s.add(chat)
        await s.commit()
        await s.refresh(chat)
        return chat.id


async def test_cap_blocks_fourth_chat_and_surfaces_active_ids() -> None:
    """Three succeed, fourth raises with active_chat_ids populated."""
    async with instance_fixture(instance_status=None) as f:
        user, project, chat1 = f["user"], f["project"], f["chat"]
        # Two more chats owned by the same user.
        chat2_id = await _new_chat_for_user(user.id, project.id)
        chat3_id = await _new_chat_for_user(user.id, project.id)
        chat4_id = await _new_chat_for_user(user.id, project.id)

        svc = InstanceService(per_user_cap=3)
        # The instance_service enqueuer default path hits arq, but with
        # a test db we use the injected enqueuer by passing a fake.
        calls: list[tuple] = []

        async def _enq(job_name: str, *args):
            calls.append((job_name, args))
            return f"job-{job_name}"

        svc._enqueue = _enq  # type: ignore[assignment]

        await svc.provision(chat1.id)
        await svc.provision(chat2_id)
        await svc.provision(chat3_id)

        with pytest.raises(PerUserCapExceeded) as exc:
            await svc.provision(chat4_id)
        assert set(exc.value.active_chat_ids) == {chat1.id, chat2_id, chat3_id}
        assert exc.value.cap == 3
        # Outer fixture's teardown path deletes Instance rows via
        # ON DELETE CASCADE when the WebChatSession rows are removed.


async def test_terminating_one_instance_frees_a_slot() -> None:
    """End a session → retry provision → succeeds."""
    async with instance_fixture(instance_status=None) as f:
        user, project, chat1 = f["user"], f["project"], f["chat"]
        chat2_id = await _new_chat_for_user(user.id, project.id)
        chat3_id = await _new_chat_for_user(user.id, project.id)
        chat4_id = await _new_chat_for_user(user.id, project.id)

        svc = InstanceService(per_user_cap=3)

        async def _enq(job_name: str, *args):
            return f"job-{job_name}"
        svc._enqueue = _enq  # type: ignore[assignment]

        inst1 = await svc.provision(chat1.id)
        await svc.provision(chat2_id)
        await svc.provision(chat3_id)

        # Fourth is blocked.
        with pytest.raises(PerUserCapExceeded):
            await svc.provision(chat4_id)

        # Terminate the first; its row flips to terminating which DOES
        # still count as active for the cap-check intentionally. We
        # need to actually drive it through the teardown job so the
        # row reaches destroyed.
        await svc.terminate(inst1.id, reason="user_request")

        # Mark the row destroyed directly so the test doesn't depend
        # on a live teardown_instance ARQ job.
        from openclow.models import Instance, async_session
        from openclow.models.instance import InstanceStatus
        from datetime import datetime, timezone
        async with async_session() as s:
            row = await s.get(Instance, inst1.id)
            row.status = InstanceStatus.DESTROYED.value
            row.terminated_at = datetime.now(timezone.utc)
            await s.commit()

        # Now the fourth provisions successfully.
        inst4 = await svc.provision(chat4_id)
        assert inst4.chat_session_id == chat4_id


async def test_raising_cap_via_platform_config_takes_effect_without_restart() -> None:
    """T053 / FR-030a — operator tunable without worker restart."""
    async with instance_fixture(instance_status=None) as f:
        user, project, chat1 = f["user"], f["project"], f["chat"]
        chat2_id = await _new_chat_for_user(user.id, project.id)
        chat3_id = await _new_chat_for_user(user.id, project.id)
        chat4_id = await _new_chat_for_user(user.id, project.id)

        svc = InstanceService(per_user_cap=3)

        async def _enq(job_name: str, *args):
            return f"job-{job_name}"
        svc._enqueue = _enq  # type: ignore[assignment]

        await svc.provision(chat1.id)
        await svc.provision(chat2_id)
        await svc.provision(chat3_id)

        # Without config override: 4th blocked.
        with pytest.raises(PerUserCapExceeded):
            await svc.provision(chat4_id)

        # With config override raising cap to 5: same service instance
        # (no restart), next provision succeeds.
        async with platform_config_override("instance", "per_user_cap", {"value": 5}):
            inst4 = await svc.provision(chat4_id)
            assert inst4.chat_session_id == chat4_id
