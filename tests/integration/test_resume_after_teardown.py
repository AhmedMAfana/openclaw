"""T066: chat resumes with in-progress code after its instance was torn down.

The user's workflow:
  * Start a container-mode chat. Make a commit on ``<chat-N>-session``.
  * Idle teardown runs (or user hits End session). Instance destroyed.
  * Come back later, send a new message.

Expected:
  * New ``Instance`` row with different UUID + slug.
  * ``session_branch`` inherited from the prior destroyed row
    (``chat.session_branch_name`` on the ``WebChatSession`` is the
    carrier — T069).
  * The host path ``/workspaces/inst-<new-slug>/`` contains the
    prior commit on the session branch (branch reattached from the
    per-project cache per FR-012 / FR-013).
  * Wall-clock from the resume message to ``status='running'`` is
    < 120 s on the warm path (SC-004 gate).

Skips unless ``OPENCLOW_DB_TESTS=1`` + real Docker + Postgres + Redis.
"""
from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("OPENCLOW_DB_TESTS") != "1",
    reason="requires Postgres + Docker + Redis; set OPENCLOW_DB_TESTS=1",
)


@pytest.mark.asyncio
async def test_resume_preserves_session_branch_commits() -> None:
    pytest.skip(
        "Pending fixture factory (see test_inactivity_reaper.py note). "
        "Assertion shape is in this module's docstring. "
        "The reattach code path lives in "
        "`WorkspaceService.reattach_session_branch` (T068) and is "
        "called from `provision_instance` between the compose render "
        "and compose up steps (T036/T069)."
    )
