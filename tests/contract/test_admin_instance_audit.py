"""Spec 003 — audit endpoint shape."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from openclow.api.serializers.admin_instance import to_audit_entry


def test_audit_entry_redacts_command():
    row = SimpleNamespace(
        actor="web_user:1:alice",
        action="force_terminate",
        command="curl -H 'Authorization: Bearer secret-token-abc-def-ghi' http://x",
        exit_code=0,
        output_summary=None,
        risk_level="dangerous",
        blocked=False,
        metadata_={"reason": "admin_forced"},
        created_at=datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
    )
    entry = to_audit_entry(row)
    assert "secret-token-abc-def-ghi" not in entry.command
    assert entry.actor == "web_user:1:alice"
    assert entry.action == "force_terminate"
    assert entry.risk_level == "dangerous"
    assert entry.metadata == {"reason": "admin_forced"}


def test_audit_entry_handles_dict_input():
    """`audit_service.get_recent` returns dicts; the serializer must accept both."""
    row = {
        "actor": "web_user:1:alice",
        "action": "extend_expiry",
        "command": "extend-expiry slug=inst-aaaaaaaaaaaaaa +4h",
        "exit_code": 0,
        "output_summary": None,
        "risk_level": "elevated",
        "blocked": False,
        "metadata_": {"hours": 4},
        "created_at": datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
    }
    entry = to_audit_entry(row)
    assert entry.action == "extend_expiry"
    assert entry.metadata == {"hours": 4}


def test_blocked_audit_entry_round_trips():
    row = SimpleNamespace(
        actor="web_user:1:alice",
        action="force_terminate",
        command="force-terminate slug=inst-... (already destroyed)",
        exit_code=0,
        output_summary=None,
        risk_level="dangerous",
        blocked=True,
        metadata_={"current_status": "destroyed"},
        created_at=datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
    )
    entry = to_audit_entry(row)
    assert entry.blocked is True
