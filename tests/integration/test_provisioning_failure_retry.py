"""T075: failure → 3 LLM attempts → failure_code → Retry resumes, Cancel cleans up.

Exercises the full Phase 9 loop end-to-end:

1. Provision a test instance whose guide.md contains a step with
   ``cmd: 'false'`` — guaranteed to fail.
2. Watch the ARQ/projctl event stream:
   * 3 ``llm_attempt`` events logged.
   * After 3 failures, the Instance row flips to ``status='failed'``
     with ``failure_code='projctl_up'`` (FR-026).
3. Tap Retry (POST ``retry_provision:<id>``). Assert ``projctl up``
   resumes from the last-successful step (NOT from step 1) per
   FR-025 — this is the projctl state.json payoff.
4. Tap Cancel on the failure screen. Assert FR-026 teardown parity:
   zero containers, zero volumes, zero Docker secrets, zero CF
   tunnel, zero DNS records, zero workspace directory for this slug.

Skips unless ``OPENCLOW_DB_TESTS=1`` + real Docker + Postgres + Redis
+ CF creds (pytest-httpx-stubbed).
"""
from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("OPENCLOW_DB_TESTS") != "1",
    reason="requires Postgres + Docker + Redis; set OPENCLOW_DB_TESTS=1",
)


@pytest.mark.asyncio
async def test_failure_triggers_three_llm_attempts_then_failed() -> None:
    pytest.skip(
        "Pending fixture factory + a test guide.md with an intentionally "
        "failing step. Assertion shape is documented in the module "
        "docstring. The retry path lives in "
        "`api/routes/assistant.py::retry_provision:<id>`; the teardown "
        "parity gate lives in `worker/tasks/instance_tasks.py::teardown_instance`."
    )


@pytest.mark.asyncio
async def test_cancel_after_failure_leaves_zero_residue() -> None:
    pytest.skip(
        "Pending fixture factory — see above. Teardown parity assertions "
        "match FR-006 and FR-026 exactly: docker ps, docker volume ls, "
        "docker secret ls, CF list-tunnels, CF list-dns-records, and "
        "os.path.exists(/workspaces/<slug>) must all return empty."
    )
