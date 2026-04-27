"""Spec 003 — contract tests for Reprovision / Rotate Token / Extend Expiry."""
from __future__ import annotations

import pytest

from openclow.api import main as api_main


def _route_methods(path: str) -> set[str]:
    out: set[str] = set()
    for r in api_main.app.routes:
        if getattr(r, "path", None) == path:
            out |= getattr(r, "methods", set()) or set()
    return out


def test_reprovision_route_registered():
    assert "POST" in _route_methods("/api/admin/instances/{slug}/reprovision")


def test_rotate_token_route_registered():
    assert "POST" in _route_methods("/api/admin/instances/{slug}/rotate-token")


def test_extend_expiry_route_registered():
    assert "POST" in _route_methods("/api/admin/instances/{slug}/extend-expiry")


def _route_response_model(path: str, method: str):
    for r in api_main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", set()) or set()):
            return r.response_model
    return None


def test_reprovision_response_model():
    from openclow.api.schemas.admin_instances import ReprovisionResponse
    assert _route_response_model("/api/admin/instances/{slug}/reprovision", "POST") is ReprovisionResponse


def test_rotate_token_response_model():
    from openclow.api.schemas.admin_instances import RotateTokenResponse
    assert _route_response_model("/api/admin/instances/{slug}/rotate-token", "POST") is RotateTokenResponse


def test_extend_expiry_response_model():
    from openclow.api.schemas.admin_instances import ExtendExpiryResponse
    assert _route_response_model("/api/admin/instances/{slug}/extend-expiry", "POST") is ExtendExpiryResponse


def test_extend_expiry_request_rejects_invalid_hours():
    from openclow.api.schemas.admin_instances import ExtendExpiryRequest

    # Allowed values
    for h in (1, 4, 24):
        ExtendExpiryRequest(extend_hours=h)
    # Forbidden — Pydantic Literal blocks them
    for bad in (2, 3, 5, 12, 48, -1, 0):
        with pytest.raises(Exception):
            ExtendExpiryRequest(extend_hours=bad)  # type: ignore[arg-type]


def test_reprovision_request_requires_confirm():
    from openclow.api.schemas.admin_instances import ReprovisionRequest

    with pytest.raises(Exception):
        ReprovisionRequest()  # type: ignore[call-arg]


def test_rotate_token_takes_no_request_body():
    """Per contract: POST /rotate-token has empty body."""
    import inspect
    from openclow.api.routes.admin_instances import admin_rotate_token

    sig = inspect.signature(admin_rotate_token)
    # Only `slug` (path) and `user` (dep) — no body parameter.
    body_params = [p for p in sig.parameters.values()
                   if p.name not in ("slug", "user")]
    assert not body_params, f"unexpected body params: {[p.name for p in body_params]}"
