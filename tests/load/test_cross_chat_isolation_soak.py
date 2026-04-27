"""T092: cross-chat isolation under sustained adversarial load.

Marked ``@pytest.mark.load``; gated by ``--run-load-tests``. Nightly
workflow (nightly-load.yml) runs this for 1 h; the scheduled long-run
job bumps to 1 week.

Setup:
  * 20 concurrent chats across 5 synthetic users (4 chats each).
  * Each chat provisions its own instance (20 live instances).
  * Rotation of adversarial prompts runs in a loop:
      - path-traversal read attempts (``/workspaces/inst-B/...``)
      - service-name forgery against ``instance_mcp``
      - cross-repo git push against a different repo URL
      - branch checkout attempts off the pinned session branch

Success gate (SC-001 / SC-009):
  * ZERO cross-chat ``audit_log`` entries over the window.
  * Every adversarial attempt is REJECTED at the MCP layer, never
    reaches the service layer.
"""
from __future__ import annotations

import os

import pytest


pytestmark = [
    pytest.mark.load,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("PYTEST_RUN_LOAD_TESTS"),
        reason="requires --run-load-tests flag (set PYTEST_RUN_LOAD_TESTS=1)",
    ),
]


@pytest.mark.asyncio
async def test_twenty_concurrent_chats_zero_cross_audit_over_window() -> None:
    """SC-001 + SC-009 gate. Body TODO when the load host is wired."""
    pytest.skip(
        "Load body requires: (a) 20 fixture_instance() context managers "
        "running concurrently via asyncio.gather, (b) an adversarial-"
        "prompt rotator that drives each chat's agent for the configured "
        "duration, (c) a final SELECT from audit_log where source_slug "
        "!= target_slug — that row count must be 0."
    )
