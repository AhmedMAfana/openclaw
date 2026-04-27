"""Spec 003 — contract tests for the per-instance detail/logs/audit endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from openclow.api.serializers.admin_instance import to_detail
from openclow.api.schemas.admin_instances import InstanceDetail


def _fake_inst(**overrides):
    base = dict(
        slug="inst-aaaaaaaaaaaaaa",
        status="failed",
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
        started_at=None,
        last_activity_at=datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc),
        grace_notification_at=None,
        terminated_at=datetime(2026, 4, 27, 10, 5, tzinfo=timezone.utc),
        terminated_reason=None,
        failure_code="compose_up",
        failure_message="docker-compose up failed: exit 1",
        chat_session=SimpleNamespace(id=7, user=SimpleNamespace(id=1, username="alice", name="alice"), user_id=1),
        project=SimpleNamespace(id=11, name="my-project"),
        tunnels=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_failed_instance_surfaces_failure_block_with_code_and_message():
    detail = to_detail(_fake_inst())
    assert detail.failure is not None
    assert detail.failure.code == "compose_up"
    assert "exit 1" in detail.failure.message


def test_running_instance_omits_failure_block():
    detail = to_detail(_fake_inst(status="running", failure_code=None, failure_message=None))
    assert detail.failure is None


def test_deleted_chat_handled_gracefully():
    detail = to_detail(_fake_inst(chat_session=None))
    assert detail.chat.deleted is True
    assert detail.chat.id is None
    assert detail.chat.link is None
    assert detail.user.deleted is True


def test_deleted_project_handled_gracefully():
    detail = to_detail(_fake_inst(project=None))
    assert detail.project.deleted is True
    assert detail.project.name == "(deleted project)"


def test_available_actions_for_failed_with_chat_offers_reprovision():
    detail = to_detail(_fake_inst())
    assert "reprovision" in detail.available_actions


def test_available_actions_for_failed_without_chat_blocks_reprovision():
    detail = to_detail(_fake_inst(chat_session=None))
    assert "reprovision" not in detail.available_actions


def test_logs_route_registered():
    from openclow.api import main as api_main

    methods = set()
    for r in api_main.app.routes:
        if getattr(r, "path", None) == "/api/admin/instances/{slug}/logs":
            methods |= getattr(r, "methods", set()) or set()
    assert "GET" in methods


def test_audit_route_registered():
    from openclow.api import main as api_main

    methods = set()
    for r in api_main.app.routes:
        if getattr(r, "path", None) == "/api/admin/instances/{slug}/audit":
            methods |= getattr(r, "methods", set()) or set()
    assert "GET" in methods


def test_instance_detail_schema_excludes_secrets():
    """Pydantic-level guard: secrets MUST NOT be defined as fields."""
    assert "heartbeat_secret" not in InstanceDetail.model_fields
    assert "db_password" not in InstanceDetail.model_fields
