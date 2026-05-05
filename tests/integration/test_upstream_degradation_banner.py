"""T081: CF tunnel breaks → banner shows; creds restored → banner clears.

FR-027a/b/c: upstream outages are banner-only — the instance row MUST
stay ``status='running'`` regardless. Auto-teardown on upstream failure
would punish users for CF/GitHub incidents they can't control.

Three observations:

1. Break CF creds inside the instance (``docker exec
   tagh-inst-<slug>-cloudflared rm /etc/cloudflared/creds.json``).
2. Within 60 s the ``tunnel_health_check_cron`` (T083) detects the
   failure and writes the Redis state key
   ``taghdev:instance_upstream:<slug>:preview_url``. The next chat
   message surfaces the ``instance_upstream_degraded`` data event
   (T084). Assert the chat banner text includes "preview URL
   temporarily unavailable" and ``instances.status == 'running'``
   (NOT ``failed``).
3. Restore creds. Within 60 s the Redis state key is deleted and the
   banner clears on the next message.

Skips unless ``OPENCLOW_DB_TESTS=1`` + real Docker + Redis.
"""
from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("OPENCLOW_DB_TESTS") != "1",
    reason="requires Postgres + Docker + Redis; set OPENCLOW_DB_TESTS=1",
)


@pytest.mark.asyncio
async def test_cf_outage_shows_banner_but_keeps_running() -> None:
    pytest.skip(
        "Pending fixture factory + a real provisioned instance. "
        "Assertion shape documented in the module docstring. "
        "The state lives in Redis at "
        "taghdev:instance_upstream:<slug>:preview_url; verify via "
        "taghdev.services.instance_service.load_upstream_state."
    )
