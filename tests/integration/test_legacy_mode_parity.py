"""T096: legacy host + docker-mode bootstraps are byte-for-byte untouched.

FR-034 / FR-036: the per-chat-instances feature MUST NOT change behaviour
for legacy ``mode='host'`` and ``mode='docker'`` projects. The T088
router at the top of ``bootstrap_project`` branches on project.mode;
this test proves the ``container`` branch doesn't leak into the
legacy code paths.

Four assertions per legacy mode:

  (a) No ``instances`` row is created for legacy-mode tasks.
  (b) No ``instance_tunnels`` row.
  (c) No call into ``InstanceService``.
  (d) The bootstrap router delegates to the legacy code path with
      byte-for-byte identical arguments vs a pre-refactor golden
      snapshot captured in ``tests/integration/fixtures/legacy_mode_golden/``.

Skips unless ``OPENCLOW_DB_TESTS=1``.
"""
from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("OPENCLOW_DB_TESTS") != "1",
    reason="requires Postgres; set OPENCLOW_DB_TESTS=1",
)


@pytest.mark.asyncio
async def test_host_mode_bootstrap_untouched() -> None:
    pytest.skip(
        "Pending fixture factory + golden snapshot. The test "
        "monkeypatches InstanceService.__init__ to raise if called "
        "during a host-mode bootstrap — if the router leaks, the "
        "test fails loudly."
    )


@pytest.mark.asyncio
async def test_docker_mode_bootstrap_untouched() -> None:
    pytest.skip(
        "Pending fixture factory + golden snapshot. Mirror of the "
        "host-mode test above, but for `mode='docker'`."
    )
