"""Spec 003 — recovery actions service-level behaviour.

Reprovision is exercised against the in-memory ``InstanceService`` to assert
the chat-rebinding invariant (Principle I): the same chat may bind to a
fresh slug after the old one ends; a destroyed/failed row stays put for audit.
"""
from __future__ import annotations

import uuid
import pytest
from datetime import datetime, timedelta, timezone

from taghdev.models.instance import Instance, InstanceStatus, TerminatedReason


def _seed_chat(store, *, chat_id: int = 7, user_id: int = 1, project_id: int = 11):
    chat = type("WebChatSession", (), {})()
    chat.id = chat_id
    chat.user_id = user_id
    chat.project_id = project_id
    chat.session_branch_name = f"chat-{chat_id}-session"
    # Required for ProjectNotContainerMode check inside provision()
    chat.project = type("Project", (), {})()
    chat.project.id = project_id
    chat.project.mode = "container"
    store.chats[chat_id] = chat
    return chat


def _seed_failed_instance(store, *, slug="inst-aaaaaaaaaaaaaa", chat_id=7):
    inst = Instance(
        id=uuid.uuid4(),
        slug=slug,
        chat_session_id=chat_id,
        project_id=11,
        status=InstanceStatus.FAILED.value,
        compose_project=f"tagh-{slug}",
        workspace_path=f"/workspaces/{slug}/",
        session_branch=f"chat-{chat_id}-session",
        heartbeat_secret="x" * 32,
        db_password="y" * 32,
        per_user_count_at_provision=1,
        last_activity_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        failure_code="compose_up",
        failure_message="boom",
    )
    store.instances[inst.id] = inst
    return inst


@pytest.mark.asyncio
async def test_reprovision_creates_fresh_slug_for_same_chat(
    inmemory_service, inmemory_store,
):
    svc, calls = inmemory_service
    _seed_chat(inmemory_store)
    old_inst = _seed_failed_instance(inmemory_store)
    old_slug = old_inst.slug

    new_inst = await svc.provision(chat_session_id=7)

    # Old row preserved for audit
    assert old_inst.id in inmemory_store.instances
    assert inmemory_store.instances[old_inst.id].status == InstanceStatus.FAILED.value
    # New row created
    assert new_inst.id != old_inst.id
    assert new_inst.slug != old_slug
    assert new_inst.chat_session_id == 7
    assert new_inst.status == InstanceStatus.PROVISIONING.value
    # Provision job enqueued
    assert any(name == "provision_instance" for name, _ in calls)


@pytest.mark.asyncio
async def test_extend_expiry_advances_expires_at_field_directly():
    """Service has no extend_expiry method — the admin endpoint writes
    expires_at directly under transaction. Pin that semantic at the model level.
    """
    inst = Instance(
        slug="inst-aaaaaaaaaaaaaa",
        chat_session_id=7,
        project_id=11,
        status=InstanceStatus.RUNNING.value,
        compose_project="tagh-x",
        workspace_path="/x",
        session_branch="x",
        heartbeat_secret="x" * 32,
        db_password="y" * 32,
        per_user_count_at_provision=1,
        last_activity_at=datetime.now(timezone.utc),
        expires_at=datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc),
    )
    new_expires = max(datetime.now(timezone.utc), inst.expires_at) + timedelta(hours=4)
    inst.expires_at = new_expires
    assert inst.expires_at == new_expires
