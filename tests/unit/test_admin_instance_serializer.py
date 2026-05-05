"""Spec 003 — regression guards for the admin instance serializer.

These tests pin the two non-negotiable Principle IV obligations that
the implementation rests on:

1. ``Instance.heartbeat_secret`` and ``Instance.db_password`` MUST never
   appear in any serialized admin response.
2. Free-text content destined for the admin UI (log lines, audit
   commands) MUST pass through ``audit_service.redact()``.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from taghdev.api.serializers.admin_instance import (
    available_actions_for_status,
    to_audit_entry,
    to_detail,
    to_list_row,
    to_log_line,
)


_NOW = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)


def _fake_user(uid=42, name="alice", admin=False):
    return SimpleNamespace(id=uid, username=name, name=name, is_admin=admin)


def _fake_chat(cid=7, user=None):
    return SimpleNamespace(id=cid, user=user or _fake_user(), user_id=42)


def _fake_project(pid=11, name="my-project"):
    return SimpleNamespace(id=pid, name=name)


def _fake_tunnel(host="abc.trycloudflare.com", status="running"):
    return SimpleNamespace(web_hostname=host, status=status)


def _fake_instance(**overrides):
    base = dict(
        id=uuid.uuid4(),
        slug="inst-aaaaaaaaaaaaaa",
        status="running",
        chat_session_id=7,
        project_id=11,
        compose_project="proj-aaaaaaaaaaaaaa",
        workspace_path="/workspaces/inst-aaaaaaaaaaaaaa",
        session_branch="chat/7-main",
        image_digest="sha256:abc",
        resource_profile="standard",
        heartbeat_secret="SUPERSECRET_HEARTBEAT_TOKEN_DO_NOT_LEAK",
        db_password="SUPERSECRET_DB_PASSWORD_DO_NOT_LEAK",
        per_user_count_at_provision=1,
        created_at=_NOW - timedelta(minutes=5),
        started_at=_NOW - timedelta(minutes=4),
        last_activity_at=_NOW - timedelta(minutes=1),
        expires_at=_NOW + timedelta(hours=24),
        grace_notification_at=None,
        terminated_at=None,
        terminated_reason=None,
        failure_code=None,
        failure_message=None,
        chat_session=_fake_chat(),
        project=_fake_project(),
        tunnels=[_fake_tunnel()],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Principle IV — secrets never serialized
# ---------------------------------------------------------------------------

def test_to_list_row_does_not_serialize_heartbeat_secret_or_db_password():
    inst = _fake_instance()
    row = to_list_row(inst)
    blob = json.dumps(row.model_dump(mode="json"))
    assert "SUPERSECRET_HEARTBEAT_TOKEN_DO_NOT_LEAK" not in blob
    assert "SUPERSECRET_DB_PASSWORD_DO_NOT_LEAK" not in blob
    assert "heartbeat_secret" not in row.model_fields
    assert "db_password" not in row.model_fields


def test_to_detail_does_not_serialize_heartbeat_secret_or_db_password():
    inst = _fake_instance()
    detail = to_detail(inst)
    blob = json.dumps(detail.model_dump(mode="json"))
    assert "SUPERSECRET_HEARTBEAT_TOKEN_DO_NOT_LEAK" not in blob
    assert "SUPERSECRET_DB_PASSWORD_DO_NOT_LEAK" not in blob
    assert "heartbeat_secret" not in detail.model_fields
    assert "db_password" not in detail.model_fields


# ---------------------------------------------------------------------------
# Redaction wrapping for log lines and audit entries
# ---------------------------------------------------------------------------

def test_to_log_line_redacts_bearer_token_in_message():
    entry = {
        "ts": _NOW.timestamp(),
        "type": "instance_log",
        "level": "info",
        "message": "outbound request Authorization: Bearer abc.def.ghi-token",
        "slug": "inst-aaaaaaaaaaaaaa",
    }
    line = to_log_line(entry)
    assert "abc.def.ghi-token" not in line.message
    assert "[REDACTED]" in line.message


def test_to_log_line_redacts_secret_in_context_dict():
    entry = {
        "ts": _NOW.timestamp(),
        "type": "instance_log",
        "level": "info",
        "message": "starting",
        "slug": "inst-aaaaaaaaaaaaaa",
        "extra": {"GITHUB_TOKEN": "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
    }
    line = to_log_line(entry)
    serialized = json.dumps(line.model_dump(mode="json"))
    assert "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in serialized


def test_to_audit_entry_redacts_command_and_output_summary():
    row = SimpleNamespace(
        actor="web_user:1:alice",
        action="force_terminate",
        command="curl -H 'Authorization: Bearer abc.def.ghi-token' https://example.com",
        exit_code=0,
        output_summary="cleaned secret aws_secret_access_key=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        risk_level="dangerous",
        blocked=False,
        metadata_={"reason": "admin_forced"},
        created_at=_NOW,
    )
    entry = to_audit_entry(row)
    blob = json.dumps(entry.model_dump(mode="json"))
    assert "abc.def.ghi-token" not in blob
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in blob


# ---------------------------------------------------------------------------
# State-machine driven action allowlist
# ---------------------------------------------------------------------------

def test_available_actions_for_running_includes_terminate_rotate_extend():
    actions = available_actions_for_status("running", has_chat=True, has_preview_url=True)
    assert "force_terminate" in actions
    assert "rotate_git_token" in actions
    assert "extend_expiry" in actions


def test_available_actions_for_failed_with_chat_includes_reprovision():
    actions = available_actions_for_status("failed", has_chat=True, has_preview_url=False)
    assert "reprovision" in actions
    assert "force_terminate" in actions
    assert "rotate_git_token" not in actions


def test_available_actions_for_destroyed_without_chat_omits_reprovision():
    actions = available_actions_for_status("destroyed", has_chat=False, has_preview_url=False)
    assert "reprovision" not in actions
    assert "open_in_chat" not in actions


def test_available_actions_for_terminating_does_not_include_extend():
    actions = available_actions_for_status("terminating", has_chat=True, has_preview_url=False)
    assert "extend_expiry" not in actions
    assert "force_terminate" not in actions  # already terminating
