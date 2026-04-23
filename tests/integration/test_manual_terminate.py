"""T071: ``/terminate`` + confirm → instance destroyed → next message refreshes.

Three assertions:

1. Within 1 s of ``end_session_confirm:<chat_session_id>``,
   ``instances.status == 'terminating'`` and ``terminated_reason ==
   'user_request'``.
2. Within 30 s the teardown ARQ job completes: ``status == 'destroyed'``
   and ``docker ps --filter label=com.docker.compose.project=tagh-<slug>``
   returns no containers.
3. Sending any new message from the same chat re-enters ``provision``
   (same code path as first-time). Different UUID, different slug.

Skips unless ``OPENCLOW_DB_TESTS=1`` + real Docker + Postgres + Redis.
"""
from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("OPENCLOW_DB_TESTS") != "1",
    reason="requires Postgres + Docker + Redis; "
    "set OPENCLOW_DB_TESTS=1 to enable",
)


@pytest.mark.asyncio
async def test_terminate_destroys_and_next_message_reprovisions() -> None:
    pytest.skip(
        "Pending: fixture factory (see test_inactivity_reaper.py note) + "
        "httpx AsyncClient against the live FastAPI app so we can POST "
        "an end_session_confirm action and await the teardown_instance "
        "ARQ job. Assertion shape is documented in this module's "
        "docstring."
    )
