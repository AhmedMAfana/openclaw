"""Spec 003 — detail view aggregation against fake instance + activity log."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from taghdev.api.serializers.admin_instance import to_detail
from taghdev.services import activity_log


def _running_instance():
    return SimpleNamespace(
        slug="inst-aaaaaaaaaaaaaa",
        status="running",
        chat_session_id=7,
        project_id=11,
        compose_project="tagh-inst-aaaaaaaaaaaaaa",
        workspace_path="/workspaces/inst-aaaaaaaaaaaaaa",
        session_branch="chat/7-main",
        image_digest="sha256:abc",
        resource_profile="standard",
        heartbeat_secret="x" * 32,
        db_password="y" * 32,
        per_user_count_at_provision=1,
        created_at=datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
        started_at=datetime(2026, 4, 27, 10, 1, tzinfo=timezone.utc),
        last_activity_at=datetime(2026, 4, 27, 10, 2, tzinfo=timezone.utc),
        expires_at=datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc),
        grace_notification_at=None,
        terminated_at=None,
        terminated_reason=None,
        failure_code=None,
        failure_message=None,
        chat_session=SimpleNamespace(id=7, user=SimpleNamespace(id=1, username="alice", name="alice"), user_id=1),
        project=SimpleNamespace(id=11, name="my-project"),
        tunnels=[SimpleNamespace(web_hostname="abc.trycloudflare.com", status="active")],
    )


def test_running_instance_aggregates_with_preview_url_and_actions():
    detail = to_detail(_running_instance())
    assert detail.preview_url == "https://abc.trycloudflare.com"
    assert detail.tunnel.url == "https://abc.trycloudflare.com"
    assert "force_terminate" in detail.available_actions
    assert "rotate_git_token" in detail.available_actions
    assert "extend_expiry" in detail.available_actions
    assert "open_preview" in detail.available_actions


def test_aggregation_preserves_diagnostic_fields_for_admin_panel():
    detail = to_detail(_running_instance())
    assert detail.compose_project == "tagh-inst-aaaaaaaaaaaaaa"
    assert detail.workspace_path == "/workspaces/inst-aaaaaaaaaaaaaa"
    assert detail.session_branch == "chat/7-main"
    assert detail.resource_profile == "standard"


def test_activity_log_query_filters_by_slug(tmp_path):
    log_file = tmp_path / "activity.jsonl"
    with open(log_file, "w") as f:
        f.write(json.dumps({"ts": 1714214400.0, "type": "instance_log", "level": "info",
                            "message": "hello A", "slug": "inst-aaaaaaaaaaaaaa"}) + "\n")
        f.write(json.dumps({"ts": 1714214401.0, "type": "instance_log", "level": "info",
                            "message": "hello B", "slug": "inst-bbbbbbbbbbbbbb"}) + "\n")
        f.write(json.dumps({"ts": 1714214402.0, "type": "instance_log", "level": "info",
                            "message": "hello A again", "slug": "inst-aaaaaaaaaaaaaa"}) + "\n")
    with patch.object(activity_log, "LOG_FILE", str(log_file)):
        rows = activity_log.query(filters={"slug": "inst-aaaaaaaaaaaaaa"}, last_n=50)
    assert len(rows) == 2
    assert all(r["slug"] == "inst-aaaaaaaaaaaaaa" for r in rows)
