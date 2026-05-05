"""Spec 003 — bulk-terminate cap and request shape."""
from __future__ import annotations

import pytest

from taghdev.api.schemas.admin_instances import (
    BulkTerminateRequest,
    BulkTerminateOutcome,
    BulkTerminateResponse,
)


def test_bulk_request_requires_confirm_field():
    with pytest.raises(Exception):
        BulkTerminateRequest(slugs=["inst-aaaaaaaaaaaaaa"])  # type: ignore[call-arg]


def test_bulk_request_accepts_one_slug():
    body = BulkTerminateRequest(slugs=["inst-aaaaaaaaaaaaaa"], confirm=True)
    assert body.slugs == ["inst-aaaaaaaaaaaaaa"]


def test_bulk_request_accepts_50_slugs():
    body = BulkTerminateRequest(
        slugs=[f"inst-{'a' * 14}"] * 50,
        confirm=True,
    )
    assert len(body.slugs) == 50


def test_bulk_outcome_envelope_has_per_slug_results():
    resp = BulkTerminateResponse(results=[
        BulkTerminateOutcome(slug="a", outcome="queued"),
        BulkTerminateOutcome(slug="b", outcome="already_ended", blocked=True),
        BulkTerminateOutcome(slug="c", outcome="not_found"),
    ])
    dumped = resp.model_dump()
    assert dumped["results"][0]["outcome"] == "queued"
    assert dumped["results"][1]["blocked"] is True
    assert dumped["results"][2]["outcome"] == "not_found"


def test_bulk_cap_constant_is_50():
    """Pin the 50 cap so it can't drift without conscious change."""
    from taghdev.api.routes import admin_instances
    assert admin_instances._BULK_CAP == 50
