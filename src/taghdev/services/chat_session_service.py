"""Chat session lifecycle — retention cascade on chat delete (FR-013a/b/c).

Spec: specs/001-per-chat-instances/tasks.md T086; research.md §10.

When a user deletes a chat, four concerns must be addressed:

  1. **Live instance teardown.** An active ``Instance`` bound to this
     chat must be torn down synchronously — removing the chat row
     while containers are still running would orphan Docker
     resources, a CF tunnel, and DNS records.
  2. **Tabular FK cascade.** The chat row's ``ON DELETE CASCADE`` FKs
     remove ``instances`` / ``instance_tunnels`` / ``tasks`` /
     ``web_chat_messages`` rows automatically.
  3. **Audit log cleanup.** ``audit_log`` rows reference instance by
     ``instance_slug`` — a text column, NOT an FK — so the cascade
     does not reach them. A service-level ``DELETE WHERE slug IN (...)``
     is required.
  4. **Branch GC.** The chat's session branch lives in the per-project
     worktree cache. We enqueue an ARQ job to prune it later so
     delete is not gated on a multi-second git operation.

``delete_chat_cascade(chat_session_id)`` implements all four. Idempotent
on repeat calls (already-deleted rows are a no-op) per Principle VI.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select

from taghdev.models import async_session
from taghdev.models.instance import Instance, InstanceStatus
from taghdev.models.web_chat import WebChatSession
from taghdev.services.instance_service import InstanceService
from taghdev.utils.logging import get_logger

log = get_logger()


async def delete_chat_cascade(chat_session_id: int) -> dict:
    """T086 — terminate the chat's instance, delete the chat, clean audit.

    Returns a summary dict for the caller to surface in an API response:

    ```python
    {"chat_session_id": 42,
     "terminated_instance": "inst-deadbeef0123",
     "audit_deleted": 17,
     "branch_gc_enqueued": True}
    ```
    """
    terminated_slug: str | None = None
    audit_deleted = 0
    branch_gc_enqueued = False
    slugs_seen: set[str] = set()
    project_id: int | None = None
    session_branch: str | None = None

    async with async_session() as session:
        chat = await session.get(WebChatSession, chat_session_id)
        if chat is None:
            log.info(
                "chat_session_service.delete_noop_missing",
                chat_session_id=chat_session_id,
            )
            return {
                "chat_session_id": chat_session_id,
                "terminated_instance": None,
                "audit_deleted": 0,
                "branch_gc_enqueued": False,
            }
        project_id = chat.project_id
        session_branch = chat.session_branch_name

        # 1. Find ALL instances for this chat (active + terminal) so we
        # can both terminate the active one and cover every slug in the
        # audit cleanup query.
        all_insts = (await session.execute(
            select(Instance).where(Instance.chat_session_id == chat_session_id)
        )).scalars().all()
        for inst in all_insts:
            slugs_seen.add(inst.slug)
            if inst.status in (
                InstanceStatus.PROVISIONING.value,
                InstanceStatus.RUNNING.value,
                InstanceStatus.IDLE.value,
                InstanceStatus.TERMINATING.value,
            ) and terminated_slug is None:
                terminated_slug = inst.slug

    # Phase-out active instance FIRST, before deleting the chat row.
    # If we delete the row first, the teardown job would fail its
    # `chat_session_id` FK lookup.
    if all_insts:
        svc = InstanceService()
        for inst in all_insts:
            if inst.status in (
                InstanceStatus.PROVISIONING.value,
                InstanceStatus.RUNNING.value,
                InstanceStatus.IDLE.value,
            ):
                try:
                    await svc.terminate(inst.id, reason="chat_deleted")
                except Exception as e:
                    log.warning(
                        "chat_session_service.terminate_failed",
                        slug=inst.slug, error=str(e)[:200],
                    )

    # 2. Delete the chat row. FK cascade handles tunnels / tasks /
    # messages. Instances have ondelete='CASCADE' per the data-model
    # so they go too (the terminate() above is what cleans the
    # Docker / CF side-effects — the DB row itself is just a record).
    async with async_session() as session:
        chat = await session.get(WebChatSession, chat_session_id)
        if chat is not None:
            await session.delete(chat)
            await session.commit()

    # 3. Audit log cleanup. The AuditLog row references instance by
    # `instance_slug` — a text column with no FK, so the cascade does
    # not reach it. Scope the delete to the slugs this chat has owned.
    if slugs_seen:
        audit_deleted = await _delete_audit_for_slugs(slugs_seen)

    # 4. Branch GC. Non-blocking — enqueue and return.
    if project_id is not None and session_branch:
        try:
            from taghdev.services.bot_actions import enqueue_job
            await enqueue_job(
                "gc_session_branch", int(project_id), session_branch
            )
            branch_gc_enqueued = True
        except Exception as e:
            log.warning(
                "chat_session_service.gc_enqueue_failed",
                project_id=project_id, branch=session_branch,
                error=str(e)[:200],
            )

    log.info(
        "chat_session_service.deleted",
        chat_session_id=chat_session_id,
        terminated_slug=terminated_slug,
        audit_deleted=audit_deleted,
        branch_gc_enqueued=branch_gc_enqueued,
    )
    return {
        "chat_session_id": chat_session_id,
        "terminated_instance": terminated_slug,
        "audit_deleted": audit_deleted,
        "branch_gc_enqueued": branch_gc_enqueued,
    }


async def _delete_audit_for_slugs(slugs: set[str]) -> int:
    """Delete AuditLog rows whose ``instance_slug`` is in the set.

    The ``instance_slug`` column was added by migration 013. Callers
    depend on this cleanup step completing — it's the last hop of the
    FR-013b cascade — so we do NOT swallow errors here; a schema drift
    that breaks it MUST surface loudly, not silently.
    """
    from taghdev.models.audit import AuditLog
    async with async_session() as session:
        res = await session.execute(
            delete(AuditLog).where(AuditLog.instance_slug.in_(slugs))
        )
        await session.commit()
        return int(res.rowcount or 0)
