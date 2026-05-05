"""Spec 003 — Recent Failures (24h) strip query."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from taghdev.api.serializers.admin_instance import to_list_row


def _failed(slug: str, when: datetime):
    return SimpleNamespace(
        slug=slug,
        status="failed",
        chat_session_id=7,
        project_id=11,
        compose_project=f"tagh-{slug}",
        workspace_path=f"/workspaces/{slug}/",
        session_branch="x",
        image_digest=None,
        resource_profile="standard",
        heartbeat_secret="x" * 32,
        db_password="y" * 32,
        per_user_count_at_provision=1,
        created_at=when,
        started_at=None,
        last_activity_at=when,
        expires_at=when + timedelta(hours=24),
        grace_notification_at=None,
        terminated_at=when,
        terminated_reason=None,
        failure_code="compose_up",
        failure_message="boom",
        chat_session=SimpleNamespace(id=7, user=SimpleNamespace(id=1, username="alice", name="alice"), user_id=1),
        project=SimpleNamespace(id=11, name="my-project"),
        tunnels=[],
    )


def test_to_list_row_failed_instance_for_recent_failures_strip():
    """Strip uses the same list endpoint with `status=failed&sort=created_at&dir=desc&limit=5`.
    The serializer must surface the project name and timestamp the strip needs.
    """
    inst = _failed("inst-aaaaaaaaaaaaaa", datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc))
    row = to_list_row(inst)
    assert row.status == "failed"
    assert row.project.name == "my-project"
    assert row.created_at == datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc)
    assert row.preview_url is None  # No tunnel for a failed instance


def test_failed_rows_sortable_by_created_at_descending():
    """Sanity — Python sort over timestamp produces the order the UI needs."""
    rows = [
        to_list_row(_failed(f"inst-{'a' * 14}", datetime(2026, 4, 27, 8, 0, tzinfo=timezone.utc))),
        to_list_row(_failed(f"inst-{'b' * 14}", datetime(2026, 4, 27, 11, 0, tzinfo=timezone.utc))),
        to_list_row(_failed(f"inst-{'c' * 14}", datetime(2026, 4, 27, 9, 30, tzinfo=timezone.utc))),
    ]
    rows.sort(key=lambda r: r.created_at, reverse=True)
    times = [r.created_at.isoformat() for r in rows]
    assert times == [
        "2026-04-27T11:00:00+00:00",
        "2026-04-27T09:30:00+00:00",
        "2026-04-27T08:00:00+00:00",
    ]
