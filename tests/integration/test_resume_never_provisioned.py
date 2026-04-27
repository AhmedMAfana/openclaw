"""T067: first-time chat follows the same path as resume.

Principle VI: no special-case branching between "new chat" and
"returning chat". Both go through ``InstanceService.get_or_resume``;
both end up in ``provision`` when no active row exists. The only
difference is that a first-time chat's ``_load_prior_session_branch``
returns ``None`` and the default ``chat-<id>-session`` naming kicks
in — documented in T069.

This test:
  * Creates a brand-new ``WebChatSession`` on a ``mode='container'``
    project. No prior ``Instance`` row.
  * Sends a message via the assistant endpoint.
  * Asserts provisioning goes through the same code path as resume:
    the trace shows one call to ``get_or_resume`` → one call to
    ``provision``. No branch called "first_time_only".

Skips unless ``OPENCLOW_DB_TESTS=1``.
"""
from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("OPENCLOW_DB_TESTS") != "1",
    reason="requires Postgres + Docker + Redis; set OPENCLOW_DB_TESTS=1",
)


@pytest.mark.asyncio
async def test_first_message_on_new_chat_provisions_via_get_or_resume() -> None:
    pytest.skip(
        "Pending fixture factory. The test observes call_count on a "
        "monkeypatched `InstanceService.provision` and "
        "`get_or_resume` to assert both are entered exactly once and "
        "that the `_load_prior_session_branch` call returned None."
    )
