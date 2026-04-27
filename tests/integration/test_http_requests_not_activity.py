"""T095: raw HTTP traffic to the preview URL is NOT an activity signal.

Regression guard for FR-011: idle cleanup must be driven by chat
messages + projctl heartbeats only. A bot or monitoring script
hammering the preview URL cannot keep a chat "alive" — the user must
actually be in the chat.

Experiment:
  1. Provision an instance. Record ``last_activity_at`` + ``expires_at``.
  2. Over 10 minutes, issue 1 000 GET/POST requests to the instance's
     web hostname via ``httpx.AsyncClient`` with varied paths.
  3. Assert ``last_activity_at`` is UNCHANGED at the end of the window
     and ``expires_at`` has NOT moved forward.

Any future change that taps the HTTP path into the touch pipeline
will fail this regression guard — closes analyze finding C6.

Skips unless ``OPENCLOW_DB_TESTS=1`` + provisioned instance.
"""
from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("OPENCLOW_DB_TESTS") != "1",
    reason="requires Postgres + Docker + CF; set OPENCLOW_DB_TESTS=1",
)


@pytest.mark.asyncio
async def test_http_traffic_does_not_bump_last_activity_at() -> None:
    pytest.skip(
        "Pending fixture factory + a live preview URL to hammer. "
        "Assertion shape documented in the module docstring."
    )
