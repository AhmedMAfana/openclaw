"""Spec 003 — contract tests for the admin Instances list endpoints.

Asserts the FastAPI router shape matches contracts/admin-instances-api.md
without requiring a live Postgres. We introspect the route metadata for
methods, path templates, status-coded responses, and dependency wiring.
"""
from __future__ import annotations

import pytest

from openclow.api import main as api_main


def _routes_by_path() -> dict[str, list]:
    """Return path → list of (method, route) tuples for all admin routes."""
    out: dict[str, list] = {}
    for route in api_main.app.routes:
        path = getattr(route, "path", None)
        if not path or not path.startswith("/api/admin/instances"):
            continue
        for m in (getattr(route, "methods", None) or set()):
            out.setdefault(path, []).append((m, route))
    return out


def test_list_route_registered():
    routes = _routes_by_path()
    assert any(m == "GET" for m, _ in routes.get("/api/admin/instances", []))


def test_summary_route_registered():
    routes = _routes_by_path()
    assert any(m == "GET" for m, _ in routes.get("/api/admin/instances/summary", []))


def test_detail_route_registered():
    routes = _routes_by_path()
    assert any(m == "GET" for m, _ in routes.get("/api/admin/instances/{slug}", []))


def test_logs_route_registered():
    routes = _routes_by_path()
    assert any(m == "GET" for m, _ in routes.get("/api/admin/instances/{slug}/logs", []))


def test_audit_route_registered():
    routes = _routes_by_path()
    assert any(m == "GET" for m, _ in routes.get("/api/admin/instances/{slug}/audit", []))


def test_response_models_match_contract():
    """Ensure each endpoint declares the expected response_model class on its
    FastAPI route registration."""
    from openclow.api.schemas.admin_instances import (
        InstancesListResponse,
        StatusCounts,
        InstanceDetail,
        InstanceLogsResponse,
        InstanceAuditResponse,
    )

    expectations = {
        ("/api/admin/instances", "GET"): InstancesListResponse,
        ("/api/admin/instances/summary", "GET"): StatusCounts,
        ("/api/admin/instances/{slug}", "GET"): InstanceDetail,
        ("/api/admin/instances/{slug}/logs", "GET"): InstanceLogsResponse,
        ("/api/admin/instances/{slug}/audit", "GET"): InstanceAuditResponse,
    }
    for (path, method), expected in expectations.items():
        match = None
        for r in api_main.app.routes:
            if getattr(r, "path", None) == path and method in (getattr(r, "methods", set()) or set()):
                match = r
                break
        assert match is not None, f"route not registered: {method} {path}"
        assert match.response_model is expected, (
            f"{method} {path}: expected response_model={expected.__name__}, got {match.response_model}"
        )


def test_status_filter_validation_raises_400_on_unknown():
    """Unknown status name should be a 400 with code=validation_error."""
    from fastapi import HTTPException
    from openclow.api.routes.admin_instances import _parse_status_param

    with pytest.raises(HTTPException) as ex:
        _parse_status_param(["bogus"])
    assert ex.value.status_code == 400
    assert ex.value.detail["code"] == "validation_error"


def test_status_filter_default_is_active_set():
    from openclow.api.routes.admin_instances import _parse_status_param

    out = _parse_status_param(None)
    assert out == {"provisioning", "running", "idle", "terminating"}


def test_status_filter_accepts_comma_separated_value():
    from openclow.api.routes.admin_instances import _parse_status_param

    out = _parse_status_param(["failed,destroyed"])
    assert out == {"failed", "destroyed"}
