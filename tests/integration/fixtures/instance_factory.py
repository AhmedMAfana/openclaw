"""DB-backed fixture factory for per-chat-instance integration tests.

The skipped scaffolds across Phase 3/4/5/6/9/N all need the same
four-row baseline:

  1. A ``User`` (so the per-user cap check has a subject).
  2. A ``Project`` with ``mode='container'`` (the router flip only
     fires in container mode).
  3. A ``WebChatSession`` tied to that user + project (the
     authoritative "chat" the instance binds to).
  4. Optionally an ``Instance`` row with a chosen ``status`` so
     tests can start from the exact state they want (``running``
     for touch / reaper tests, ``destroyed`` for resume tests,
     etc.).

The factory cleans up everything it creates in a ``finally`` so
passing or failing tests don't pollute the DB.

Skips at import time unless ``OPENCLOW_DB_TESTS=1`` and a reachable
Postgres is wired via ``settings.database_url``. Individual test
modules that are already guarded by the same env var can call these
helpers unconditionally.
"""
from __future__ import annotations

import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from sqlalchemy import delete

from openclow.models import (
    Instance,
    PlatformConfig,
    Project,
    User,
    WebChatSession,
    async_session,
)
from openclow.models.instance import InstanceStatus
from openclow.models.instance_tunnel import InstanceTunnel
from openclow.services.credentials_service import CredentialsService


def _env_enabled() -> bool:
    return os.environ.get("OPENCLOW_DB_TESTS") == "1"


async def _uniq() -> str:
    """Suffix for fixture object names — collision-safe inside a suite."""
    return secrets.token_hex(4)


@asynccontextmanager
async def instance_fixture(
    *,
    instance_status: str | None = "running",
    session_branch: str | None = None,
    is_admin: bool = False,
) -> AsyncIterator[dict]:
    """Create User + Project + WebChatSession + (optional) Instance.

    Yields a dict::

        {
          "user":    User,
          "project": Project,
          "chat":    WebChatSession,
          "instance": Instance | None,
        }

    Tears down by ID on exit (cascade FKs handle tunnels / tasks /
    messages). Audit rows keyed by ``instance_slug`` are left alone —
    that's chat_session_service's responsibility in prod; tests that
    verify audit cleanup should do it in their assertion block.

    Args:
      instance_status:  initial status for the optional Instance row.
                        Pass ``None`` to skip creating one (useful for
                        first-time-chat tests like T067).
      session_branch:   override the auto-generated session-branch name.
                        Tests for resume after teardown (T066) want to
                        pin this so the assertion can check the name
                        carries forward.
      is_admin:         admin-flag on the fixture User.
    """
    if not _env_enabled():
        raise RuntimeError(
            "instance_fixture() called without OPENCLOW_DB_TESTS=1. "
            "Guard the test with the same env check."
        )

    suffix = await _uniq()
    slug = f"inst-{secrets.token_hex(7)}"
    user: User | None = None
    project: Project | None = None
    chat: WebChatSession | None = None
    instance: Instance | None = None

    try:
        async with async_session() as s:
            user = User(
                chat_provider_type="test",
                chat_provider_uid=f"test-uid-{suffix}",
                username=f"fixture-{suffix}",
                is_allowed=True,
                is_admin=is_admin,
            )
            s.add(user)
            await s.commit()
            await s.refresh(user)

            project = Project(
                name=f"fixture-{suffix}",
                github_repo=f"org/fixture-{suffix}",
                default_branch="main",
                tech_stack="laravel-vue",
                mode="container",
                is_dockerized=True,
                tunnel_enabled=True,
                status="active",
            )
            s.add(project)
            await s.commit()
            await s.refresh(project)

            chat = WebChatSession(
                user_id=user.id,
                project_id=project.id,
                title=f"Fixture chat {suffix}",
                session_branch_name=session_branch,
            )
            s.add(chat)
            await s.commit()
            await s.refresh(chat)

            if instance_status is not None:
                now = datetime.now(timezone.utc)
                branch = session_branch or f"chat-{chat.id}-session"
                instance = Instance(
                    id=uuid.uuid4(),
                    slug=slug,
                    chat_session_id=chat.id,
                    project_id=project.id,
                    status=instance_status,
                    compose_project=f"tagh-{slug}",
                    workspace_path=f"/workspaces/{slug}/",
                    session_branch=branch,
                    heartbeat_secret=CredentialsService.generate_heartbeat_secret(),
                    db_password=CredentialsService.generate_db_password(),
                    per_user_count_at_provision=1,
                    last_activity_at=now,
                    expires_at=now + timedelta(hours=24),
                    started_at=now if instance_status == InstanceStatus.RUNNING.value else None,
                )
                s.add(instance)
                await s.commit()
                await s.refresh(instance)

        yield {
            "user": user,
            "project": project,
            "chat": chat,
            "instance": instance,
        }
    finally:
        # Teardown in reverse FK order. FK cascades handle the rest.
        async with async_session() as s:
            if chat is not None and chat.id is not None:
                await s.execute(
                    delete(InstanceTunnel).where(
                        InstanceTunnel.instance_id.in_(
                            await _scalars(s, Instance.id, Instance.chat_session_id == chat.id)
                        )
                    )
                )
                await s.execute(
                    delete(Instance).where(Instance.chat_session_id == chat.id)
                )
                await s.execute(
                    delete(WebChatSession).where(WebChatSession.id == chat.id)
                )
            if project is not None and project.id is not None:
                await s.execute(
                    delete(Project).where(Project.id == project.id)
                )
            if user is not None and user.id is not None:
                await s.execute(
                    delete(User).where(User.id == user.id)
                )
            await s.commit()


async def _scalars(session, col, where):
    """Return a list of scalar values for a simple column+where query.

    Used only inside teardown so the instance FK cascade can identify
    which tunnel rows to delete without loading full entities.
    """
    from sqlalchemy import select as _select
    rows = (await session.execute(_select(col).where(where))).all()
    return [r[0] for r in rows]


@asynccontextmanager
async def platform_config_override(
    category: str, key: str, value: dict,
) -> AsyncIterator[None]:
    """Temporarily set a ``platform_config`` row for the duration of a test.

    Restores any prior value on exit so the test is non-destructive
    against a populated DB. Useful for T034's "without restart" leg
    and T053's idle-TTL override test.
    """
    if not _env_enabled():
        raise RuntimeError("platform_config_override without OPENCLOW_DB_TESTS=1")

    prior_value: dict | None = None
    row_existed = False
    async with async_session() as s:
        res = await s.execute(
            PlatformConfig.__table__.select().where(
                PlatformConfig.category == category,
                PlatformConfig.key == key,
            )
        )
        row = res.first()
        if row is not None:
            prior_value = dict(row._mapping["value"])
            row_existed = True
            await s.execute(
                PlatformConfig.__table__.update()
                .where(
                    PlatformConfig.category == category,
                    PlatformConfig.key == key,
                )
                .values(value=value, is_active=True)
            )
        else:
            s.add(PlatformConfig(category=category, key=key, value=value, is_active=True))
        await s.commit()

    try:
        yield None
    finally:
        async with async_session() as s:
            if row_existed:
                await s.execute(
                    PlatformConfig.__table__.update()
                    .where(
                        PlatformConfig.category == category,
                        PlatformConfig.key == key,
                    )
                    .values(value=prior_value)
                )
            else:
                await s.execute(
                    PlatformConfig.__table__.delete().where(
                        PlatformConfig.category == category,
                        PlatformConfig.key == key,
                    )
                )
            await s.commit()
