"""T061: GitHub installation token scopes push to one repo.

Defence-in-depth on top of T040's branch-pinning git_mcp (Principle I).
The scenario:

1. Provision ``inst-A`` bound to repo ``org/acme-A``. Mint its GitHub
   installation token via ``CredentialsService.github_push_token``.
2. Inside the instance's workspace, mutate the git remote URL to
   ``https://github.com/org/acme-B``.
3. Attempt a ``git push``. GitHub MUST reject at the auth layer
   (HTTP 403) because the installation token's ``repositories`` claim
   does not include ``acme-B``.
4. Audit log MUST record the failure with ``event='push_unauthorized'``
   + ``{instance_slug: inst-A, attempted_repo: org/acme-B}``.

Skips unless a real GitHub App is configured and
``OPENCLOW_GITHUB_TESTS=1`` is set — the test needs a live token
exchange so a mocked HTTP layer would not exercise the actual
security boundary.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENCLOW_GITHUB_TESTS") != "1",
    reason="requires a live GitHub App; set OPENCLOW_GITHUB_TESTS=1 to enable",
)


@pytest.mark.asyncio
async def test_push_to_unrelated_repo_rejected_by_github() -> None:
    pytest.skip(
        "Pending: fixture app + two repos ``org/acme-A`` / ``org/acme-B`` "
        "installed on the GitHub App. The assertion shape is documented "
        "in this module's docstring; run path is `git push` under a "
        "mutated remote URL; expect 403 and a redacted audit row."
    )
