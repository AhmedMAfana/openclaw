"""Spec 003 — contract tests for the Force Terminate endpoint."""
from __future__ import annotations

import pytest

from openclow.api import main as api_main


def _route_methods(path: str) -> set[str]:
    out: set[str] = set()
    for r in api_main.app.routes:
        if getattr(r, "path", None) == path:
            out |= getattr(r, "methods", set()) or set()
    return out


def test_terminate_route_registered():
    assert "POST" in _route_methods("/api/admin/instances/{slug}/terminate")


def test_bulk_terminate_route_registered():
    assert "POST" in _route_methods("/api/admin/instances/bulk-terminate")


def _route_response_model(path: str, method: str):
    for r in api_main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", set()) or set()):
            return r.response_model
    return None


def test_terminate_response_model():
    from openclow.api.schemas.admin_instances import TerminateResponse

    assert _route_response_model("/api/admin/instances/{slug}/terminate", "POST") is TerminateResponse


def test_bulk_terminate_response_model():
    from openclow.api.schemas.admin_instances import BulkTerminateResponse

    assert _route_response_model("/api/admin/instances/bulk-terminate", "POST") is BulkTerminateResponse


def test_terminate_request_requires_confirm_field():
    from openclow.api.schemas.admin_instances import TerminateRequest

    with pytest.raises(Exception):
        TerminateRequest()  # type: ignore[call-arg]
    body = TerminateRequest(confirm=True, note="manual cleanup")
    assert body.confirm is True
    assert body.note == "manual cleanup"


def test_bulk_terminate_envelope_shape():
    from openclow.api.schemas.admin_instances import (
        BulkTerminateOutcome,
        BulkTerminateResponse,
    )

    resp = BulkTerminateResponse(
        results=[
            BulkTerminateOutcome(slug="inst-aaaaaaaaaaaaaa", outcome="queued"),
            BulkTerminateOutcome(slug="inst-bbbbbbbbbbbbbb", outcome="already_ended", blocked=True),
            BulkTerminateOutcome(slug="inst-cccccccccccccc", outcome="not_found"),
        ],
    )
    dumped = resp.model_dump()
    assert {r["outcome"] for r in dumped["results"]} == {"queued", "already_ended", "not_found"}


def test_no_op_terminate_envelope_marks_blocked_with_reason():
    from types import SimpleNamespace
    from openclow.api.routes.admin_instances import _no_op_terminate_envelope

    inst = SimpleNamespace(slug="inst-aaaaaaaaaaaaaa", status="destroyed")
    env = _no_op_terminate_envelope(inst)
    assert env.blocked is True
    assert env.status == "destroyed"
    assert env.reason == "already_ended"
