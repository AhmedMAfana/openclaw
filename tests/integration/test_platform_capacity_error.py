"""T034a: PlatformAtCapacity is user-distinguishable from PerUserCapExceeded.

FR-030 vs FR-030a prove: the two capacity errors carry different
chat-facing text AND different navigation affordances.

The test monkey-patches ``InstanceService``'s capacity guard to raise
``PlatformAtCapacity`` regardless of actual host resources, then checks
the chat-rendered message:

  * contains the phrase "try again later"           (FR-030 retry guidance)
  * does NOT contain "too many active chats"        (that belongs to FR-030a)
  * does NOT carry per-chat navigation buttons      (FR-030 vs FR-030b)

Requires the ``chat_task.py`` error translator from T044. Until it lands,
the whole module skips cleanly.
"""
from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


_chat_task = pytest.importorskip(
    "openclow.worker.tasks.chat_task",
    reason="chat_task.py exists but T044 error translator not landed yet",
)


@pytest.fixture
def force_platform_at_capacity(monkeypatch):
    """Monkey-patch InstanceService.provision's capacity guard.

    The guard callable is held on the InstanceService instance used by
    chat_task. T044's implementation decides whether to let us inject
    via a module-level singleton or a dependency-injection point; the
    fixture is parameterised to work with either.
    """
    pytest.skip(
        "fixture requires T044 exposing the InstanceService capacity_guard "
        "as an injection point on chat_task"
    )


async def test_error_message_says_try_again_later(
    force_platform_at_capacity,
):
    pytest.skip("depends on T044 chat_task PlatformAtCapacity translation")


async def test_error_message_does_not_mention_too_many_active_chats(
    force_platform_at_capacity,
):
    """FR-030 must not crib FR-030a's wording."""
    pytest.skip("depends on T044 chat_task PlatformAtCapacity translation")


async def test_error_payload_has_no_per_chat_navigation_menu(
    force_platform_at_capacity,
):
    """FR-030 vs FR-030b: only the per-user-cap error carries per-chat links."""
    pytest.skip("depends on T044 chat_task PlatformAtCapacity translation")
