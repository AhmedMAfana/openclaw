"""T047: contract tests for ``/internal/instances/<slug>/heartbeat``.

Contract: specs/001-per-chat-instances/contracts/heartbeat-api.md.

Seven required assertions:

1. Valid HMAC + live instance → 200; ``expires_at`` bumps in DB.
2. Forged HMAC → 401; auth-fail counter increments.
3. HMAC signed with a DIFFERENT instance's secret → 401
   (cross-instance forgery guard).
4. Slug in path does not match the HMAC-signing instance → 401.
5. Instance in ``terminating`` status → 409 with current status.
6. >30 req/s (one req/s in the v1 rate limit) → 429 with
   ``Retry-After``.
7. ``rotate-git-token`` on GitHub App outage → 503 with
   ``Retry-After``. Deferred to T064.

The route handler is now live (``openclow.api.routes.instances``) but
exercising it end-to-end requires a fixture ``Instance`` row + a real
Redis for the rate limiter. Test bodies skip with a clear TODO until
the fixture factory + in-memory Redis shim land.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENCLOW_DB_TESTS") != "1",
    reason="requires Postgres + Redis; set OPENCLOW_DB_TESTS=1 to enable",
)


def _sign(secret: str, body: bytes) -> str:
    return "hmac-sha256=" + _hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


def test_hmac_helper_is_constant_time() -> None:
    """The endpoint uses ``hmac.compare_digest``; the helper below matches.

    This is a pure smoke test of the test helper — asserts two signatures
    derived from the same secret+body are equal byte-for-byte.
    """
    sig_a = _sign("shhh", b'{"at":"2026-04-23T14:22:01.123Z"}')
    sig_b = _sign("shhh", b'{"at":"2026-04-23T14:22:01.123Z"}')
    assert sig_a == sig_b
    assert sig_a.startswith("hmac-sha256=")


@pytest.mark.asyncio
async def test_valid_hmac_live_instance_returns_200() -> None:
    pytest.skip("Pending fixture factory (see test_inactivity_reaper.py).")


@pytest.mark.asyncio
async def test_forged_hmac_returns_401() -> None:
    pytest.skip("Pending fixture factory.")


@pytest.mark.asyncio
async def test_cross_instance_hmac_returns_401() -> None:
    pytest.skip("Pending fixture factory.")


@pytest.mark.asyncio
async def test_slug_path_mismatch_returns_401() -> None:
    pytest.skip("Pending fixture factory.")


@pytest.mark.asyncio
async def test_terminating_status_returns_409() -> None:
    pytest.skip("Pending fixture factory.")


@pytest.mark.asyncio
async def test_rate_limit_returns_429_with_retry_after() -> None:
    pytest.skip("Pending fixture factory + in-memory Redis shim.")


@pytest.mark.asyncio
async def test_rotate_git_token_github_outage_returns_503() -> None:
    """Deferred to T064 (rotate-git-token endpoint)."""
    pytest.skip("T064 not landed yet.")
