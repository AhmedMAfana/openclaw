"""T034: per-user concurrency cap (FR-030a) integration test.

Against a real Postgres (migration 011 applied) + Redis (for the
user-scoped provision lock):

  1. Open three chats for ``user_id=U``; provision each. All three
     succeed.
  2. Open a fourth chat; provision attempt raises
     ``PerUserCapExceeded`` with ``active_chat_ids`` populated with
     the previous three chat IDs (FR-030b navigation surface).
  3. Terminate one of the three; re-attempt the fourth chat; it
     provisions successfully (slot freed).
  4. Raise the cap via ``platform_config.set(category='instance',
     key='per_user_cap', value={'value': 5})``; a re-read takes
     effect without restarting the worker.

Requires a live Postgres + Redis. Guarded by ``OPENCLOW_DB_TESTS=1``.
"""
from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("OPENCLOW_DB_TESTS"),
        reason="per-user cap tests need OPENCLOW_DB_TESTS=1 + a live Postgres + Redis",
    ),
]


@pytest.fixture
async def three_provisioned_chats():
    """Seed (user, 3 chats, 3 active instances) into a real DB.

    Yields: (user_id, [chat_id_1, chat_id_2, chat_id_3], InstanceService)
    The fixture cleans up the user + chats + instances on teardown.
    """
    pytest.skip(
        "fixture requires a test-DB helper (users/chats/projects factories); "
        "wire alongside T036 integration harness"
    )


async def test_cap_blocks_fourth_chat_and_surfaces_active_ids(
    three_provisioned_chats,
):
    pytest.skip("depends on real-DB test fixtures")


async def test_terminating_one_instance_frees_a_slot(
    three_provisioned_chats,
):
    pytest.skip("depends on real-DB test fixtures")


async def test_raising_cap_via_platform_config_takes_effect_without_restart(
    three_provisioned_chats,
):
    """FR-030a: cap is operator-configurable 'without a code change'."""
    pytest.skip(
        "depends on real-DB test fixtures + InstanceService wiring a fresh "
        "per_user_cap read from platform_config on every provision call"
    )
