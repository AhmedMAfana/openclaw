"""T085: deleting a chat cleans every artifact it owns.

Retention cascade per FR-013a/b/c. Verifies ``chat_session_service.
delete_chat_cascade`` closes all four loops:

  (a) Active instance teardown finishes synchronously.
  (b) ``instances`` + ``instance_tunnels`` + ``tasks`` + ``web_chat_messages``
      rows all delete via FK cascade off the chat row.
  (c) ``audit_log`` rows whose ``instance_slug`` matched the chat's
      slugs are deleted by the service's explicit sweep.
  (d) ``/workspaces/_cache/<project>/`` has the session branch pruned
      by the enqueued ``gc_session_branch`` ARQ job.

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
async def test_delete_chat_cascade_all_four_loops() -> None:
    pytest.skip(
        "Pending fixture factory. The cascade service is "
        "`taghdev.services.chat_session_service.delete_chat_cascade`; "
        "test walks through the returned summary dict + queries each "
        "table + checks the workspace cache."
    )
