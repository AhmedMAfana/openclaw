"""T093: fifty concurrent instances ramp (SC-006).

Marked ``@pytest.mark.load``; gated by ``--run-load-tests``.

Ramp from 0 → 50 concurrent running instances, then hold idle for the
configured duration. Assertions:

  (a) Every one of the 50 reaches ``status='running'``.
  (b) Host RSS stays below 32 GB while all 50 idle.
  (c) No provisioning failure carries ``failure_code='out_of_capacity'``
      — the platform capacity guard MUST NOT spuriously fire at 50.

Baseline for the nightly scheduled job; doubles as a guard against
future memory-regression in compose / cloudflared / projctl.
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
async def test_ramp_to_fifty_instances_under_rss_budget() -> None:
    """SC-006 gate. Body TODO when the load host is wired."""
    pytest.skip(
        "Load body requires: (a) 10 fixture users × 5 chats each via "
        "instance_factory.instance_fixture, (b) drive provision via "
        "InstanceService.provision in async batches of 5-10, "
        "(c) poll resource.getrusage() for RSS, (d) assert all 50 "
        "rows reach status='running' within 10 min, (e) assert max "
        "RSS during steady-state < 32 GiB."
    )
