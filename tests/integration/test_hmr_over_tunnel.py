"""T054: HMR round-trip SLO (SC-005).

Given a provisioned ``mode='container'`` instance:

  1. Open a WebSocket client against ``wss://<hmr_hostname>:443``.
  2. Perform **100 sequential file edits** in
     ``/workspaces/inst-<slug>/resources/js/app.js`` — each edit
     modifies one line so Vite's file watcher emits an HMR payload.
  3. Record per-edit HMR-payload arrival latency.
  4. Assert p95 < 3 s (SC-005 "at least 95% of edit events").
  5. Assert every edit's payload eventually arrives — no silent drops.

Requires a real instance running the laravel-vue template + a live
Cloudflare Tunnel. ``OPENCLOW_E2E=1`` gate mirrors T031's.
"""
from __future__ import annotations

import os

import pytest


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("OPENCLOW_E2E"),
        reason="requires OPENCLOW_E2E=1 + live Docker + CF zone",
    ),
]


@pytest.mark.asyncio
async def test_hmr_p95_under_3_seconds_across_100_edits() -> None:
    """SC-005 gate. See module docstring for the full assertion chain."""
    pytest.skip(
        "Test body requires: "
        "(a) a provisioned instance with a live Vite dev server, "
        "(b) a WebSocket client (e.g. `websockets` library), "
        "(c) file edits issued via fixtures/instance_factory.py's "
        "workspace path. Write the body against a staging CF zone — "
        "the localhost Vite->HMR loop doesn't exercise the tunnel leg."
    )


@pytest.mark.asyncio
async def test_hmr_delivers_every_edit_with_no_drops() -> None:
    """Complement to the p95 check — no silent drops."""
    pytest.skip(
        "Same infra as the p95 test. Assert len(received_payloads) == "
        "100 at the end of the window."
    )
