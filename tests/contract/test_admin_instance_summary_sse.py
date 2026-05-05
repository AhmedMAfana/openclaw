"""Spec 003 — instance_summary SSE event shape and debounce."""
from __future__ import annotations

import time
from unittest.mock import patch


def test_summary_payload_has_all_required_fields():
    """Per contracts/sse-events.md, instance_summary carries the five status counts."""
    from taghdev.api.serializers.admin_instance import build_status_counts

    counts = build_status_counts(
        {"running": 5, "idle": 2, "provisioning": 1, "terminating": 0},
        failed_24h=3,
        used_capacity=8,
        cap=50,
    )
    snapshot = counts.model_dump()
    for k in ("running", "idle", "provisioning", "terminating", "failed_24h", "total_active", "capacity"):
        assert k in snapshot
    assert snapshot["total_active"] == 8  # 5 + 2 + 1 + 0


def test_maybe_emit_summary_debounces_to_one_per_minute():
    from taghdev.services import instance_service as svc

    # Reset the module-level debounce timestamp so we get a clean window.
    svc._last_summary_emit_at = 0.0

    emit_count = []

    def fake_log_event(evt_type, body):
        emit_count.append(evt_type)

    with patch("taghdev.services.activity_log.log_event", side_effect=fake_log_event):
        svc.maybe_emit_summary({"running": 1})
        svc.maybe_emit_summary({"running": 2})
        svc.maybe_emit_summary({"running": 3})
        # All three within <60s — only the first is emitted.
        assert emit_count.count("instance_summary") == 1


def test_emit_instance_event_never_raises_on_failure():
    from taghdev.services.instance_service import emit_instance_event

    with patch("taghdev.services.activity_log.log_event", side_effect=RuntimeError("disk full")):
        # Must not propagate — emits are best-effort by contract.
        emit_instance_event({"type": "instance_status", "slug": "inst-xxx", "status": "running"})
