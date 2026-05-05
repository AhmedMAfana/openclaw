"""T020: unit tests for the rewritten TunnelService (CF named tunnels).

Uses pytest-httpx to stub the Cloudflare v4 API. Covers:
  * Happy-path provision (create + DNS)
  * Idempotent provision (re-used tunnel, no-op DNS)
  * Destroy happy path + already-gone tolerance
  * Explicit timeout enforcement (Principle IX)
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from taghdev.services.tunnel_service import (
    CloudflareAPIError,
    CloudflareConfig,
    TunnelService,
    DEFAULT_TIMEOUT,
)


CFG = CloudflareConfig(
    account_id="acct-xyz",
    zone_id="zone-xyz",
    zone_domain="dev.example.com",
    api_token="test-token",
)

SLUG = "inst-0123456789abcd"
TUNNEL_NAME = f"tagh-{SLUG}"
TUNNEL_ID = "00000000-1111-2222-3333-444444444444"
CNAME_TARGET = f"{TUNNEL_ID}.cfargotunnel.com"


def _service(transport: httpx.MockTransport) -> TunnelService:
    """Build a TunnelService whose HTTP client uses a provided MockTransport."""
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=CFG.api_base,
            transport=transport,
            timeout=DEFAULT_TIMEOUT,
            headers={"Authorization": f"Bearer {CFG.api_token}"},
        )

    return TunnelService(CFG, http_client_factory=factory)


@pytest.mark.asyncio
async def test_provision_creates_tunnel_and_three_cnames():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/cfd_tunnel"):
            # Idempotency check: not found yet.
            return httpx.Response(200, json={"result": [], "success": True})
        if request.method == "POST" and path.endswith("/cfd_tunnel"):
            return httpx.Response(200, json={
                "result": {"id": TUNNEL_ID, "token": "tok-abc"},
                "success": True,
            })
        if request.method == "GET" and path.endswith("/dns_records"):
            return httpx.Response(200, json={"result": [], "success": True})
        if request.method == "POST" and path.endswith("/dns_records"):
            return httpx.Response(200, json={
                "result": {"id": f"rec-{calls.count(request)}"},
                "success": True,
            })
        return httpx.Response(500, json={"error": "unexpected request"})

    ts = _service(httpx.MockTransport(handler))
    result = await ts.provision(uuid.uuid4(), SLUG)

    assert result.cf_tunnel_id == TUNNEL_ID
    assert result.cf_tunnel_name == TUNNEL_NAME
    assert result.web_hostname == f"{SLUG}.dev.example.com"
    assert result.hmr_hostname == f"hmr-{SLUG}.dev.example.com"
    assert result.ide_hostname == f"ide-{SLUG}.dev.example.com"
    assert result.credentials_secret == f"{TUNNEL_NAME}-cf"
    assert result.credentials_blob == "tok-abc"

    # One POST /cfd_tunnel + three POST /dns_records (+ four GETs for idempotency).
    posts = [c for c in calls if c.method == "POST"]
    assert len(posts) == 4, [c.url.path for c in posts]


@pytest.mark.asyncio
async def test_provision_is_idempotent_on_retry():
    """If the tunnel already exists at CF, reuse it and fetch its token."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/cfd_tunnel"):
            return httpx.Response(200, json={
                "result": [{"id": TUNNEL_ID, "name": TUNNEL_NAME, "status": "healthy"}],
                "success": True,
            })
        if request.method == "GET" and "/token" in path:
            return httpx.Response(200, json={"result": "tok-recovered", "success": True})
        if request.method == "GET" and path.endswith("/dns_records"):
            # Already-present CNAMEs (content matches what we'd set).
            return httpx.Response(200, json={
                "result": [{
                    "id": "rec-existing",
                    "name": "whatever",
                    "content": CNAME_TARGET,
                }],
                "success": True,
            })
        if request.method == "POST":
            # Should NOT hit POST on a full retry.
            return httpx.Response(500, json={"error": f"unexpected POST to {path}"})
        return httpx.Response(500, json={"error": "unexpected"})

    ts = _service(httpx.MockTransport(handler))
    result = await ts.provision(uuid.uuid4(), SLUG)
    assert result.cf_tunnel_id == TUNNEL_ID
    assert result.credentials_blob == "tok-recovered"


@pytest.mark.asyncio
async def test_destroy_deletes_records_and_tunnel():
    deletes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/cfd_tunnel"):
            return httpx.Response(200, json={
                "result": [{"id": TUNNEL_ID, "name": TUNNEL_NAME}],
                "success": True,
            })
        if request.method == "GET" and path.endswith("/dns_records"):
            return httpx.Response(200, json={
                "result": [
                    {"id": "rec-1"}, {"id": "rec-2"}, {"id": "rec-3"},
                ],
                "success": True,
            })
        if request.method == "DELETE":
            deletes.append(path)
            return httpx.Response(200, json={"success": True})
        return httpx.Response(500, json={"error": "unexpected"})

    ts = _service(httpx.MockTransport(handler))
    await ts.destroy(uuid.uuid4(), SLUG)

    # Three DNS record deletes + one tunnel delete.
    assert len(deletes) == 4, deletes
    assert any("/cfd_tunnel/" in d for d in deletes), deletes


@pytest.mark.asyncio
async def test_destroy_tolerates_already_gone():
    def handler(request: httpx.Request) -> httpx.Response:
        # Tunnel already gone — GET returns empty list.
        if request.method == "GET" and request.url.path.endswith("/cfd_tunnel"):
            return httpx.Response(200, json={"result": [], "success": True})
        return httpx.Response(500, json={"error": "should not reach here"})

    ts = _service(httpx.MockTransport(handler))
    # Must not raise — teardown on a gone resource is a no-op (research.md §4).
    await ts.destroy(uuid.uuid4(), SLUG)


@pytest.mark.asyncio
async def test_cf_api_error_surfaces_with_status_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "errors": [{"code": 9109}]})

    ts = _service(httpx.MockTransport(handler))
    with pytest.raises(CloudflareAPIError) as info:
        await ts.provision(uuid.uuid4(), SLUG)
    assert info.value.status == 403


def test_default_timeout_is_explicit_per_principle_ix():
    """Principle IX: every external call carries a timeout; no unbounded waits."""
    # DEFAULT_TIMEOUT is exported — if it ever becomes None/unbounded, this fails.
    assert DEFAULT_TIMEOUT.connect == 5.0
    assert DEFAULT_TIMEOUT.read == 10.0
    assert DEFAULT_TIMEOUT.write == 10.0
    assert DEFAULT_TIMEOUT.pool == 10.0
