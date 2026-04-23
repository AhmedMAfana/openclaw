"""T031: end-to-end provision → healthcheck → teardown.

Gates per ``quickstart.md §8``:

  1. ``InstanceService.provision`` → row present, ARQ job fires.
  2. Provision ARQ job runs `docker compose up`, `projctl up`, and flips
     ``status='running'``.
  3. Tunnel row moves to ``active``; CF stub confirms tunnel + DNS created.
  4. HTTP health check passes against the instance.
  5. ``InstanceService.terminate`` → teardown job runs, and after it
     completes there is:
       * no docker compose project ``tagh-<slug>``
       * no workspace directory ``/workspaces/<slug>/``
       * no ``instance_tunnels`` row with ``status='active'``
       * no CF tunnel + DNS at the (stubbed) Cloudflare side
       * no Docker secret ``<slug>-cf``

This test is HEAVY: it requires a real Docker daemon, a real Postgres,
a real Redis, and the ARQ worker running with the ``provision_instance``
and ``teardown_instance`` jobs registered (T036 + T037). Cloudflare is
stubbed at the HTTP layer via ``httpx.MockTransport``.

Until T036/T037 land, the whole module skips cleanly rather than
failing. See ``pytestmark`` below.
"""
from __future__ import annotations

import os

import pytest

# Skip the module unless the orchestrator runtime is wired. The ARQ job
# names are the hinge: if they are importable, the rest of the stack is
# either present or will fail loudly on its own.
_provision_job = pytest.importorskip(
    "openclow.worker.tasks.instance_tasks",
    reason="T036 (provision_instance) + T037 (teardown_instance) not landed yet",
)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("OPENCLOW_E2E"),
        reason="E2E tests need OPENCLOW_E2E=1 + a live Docker + Postgres + Redis",
    ),
]


@pytest.fixture
async def provisioned_instance():
    """Run the full provision path and yield the resulting Instance.

    Teardown at yield time is the negative half of the assertion —
    a leak here is caught by `test_teardown_leaves_zero_residue`.
    """
    # Intentionally left for T036/T037 implementations to flesh out.
    # The fixture is a placeholder so test authors later know where to
    # wire the real session/project/chat factory + CF stub transport.
    pytest.skip("fixture body requires T036 + T037 + CF stub wiring")


async def test_provision_brings_stack_up(provisioned_instance):
    """Row flips to running, tunnel active, compose up."""
    pytest.skip("depends on T036 provision_instance job")


async def test_provision_is_idempotent_across_jobs(provisioned_instance):
    """Re-enqueue provision_instance on the same row: no duplicate infra."""
    pytest.skip("depends on T036 provision_instance job (research.md §4)")


async def test_hmr_reaches_over_tunnel(provisioned_instance):
    """SC-005: edit a watched file → HMR payload within 3s."""
    pytest.skip("requires running Vite + CF tunnel sandbox")


async def test_teardown_leaves_zero_residue(provisioned_instance):
    """quickstart.md §8: teardown must leave no compose / volume / CF trace."""
    pytest.skip("depends on T037 teardown_instance job")
