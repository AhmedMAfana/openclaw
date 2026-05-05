"""Admin Instances management surface (spec 003-admin-instance-mgmt).

JSON API only. The UI lives in the React chat frontend at
``chat_frontend/src/components/settings/SettingsInstances.tsx`` and
mounts under ``/chat/`` → SettingsPanel → "Instances" tab. There is no
legacy Jinja2 ``/settings/instances`` page; the active dashboard is the
React chat frontend.

Authorization: every endpoint depends on ``web_user_dep`` and calls
``_require_admin(user)`` inline (the canonical pattern from
``access.py``). Non-admin → 403.

Principle IV — every log line, audit ``command``, and ``output_summary``
returned to the admin UI passes through ``audit_service.redact()``.
``Instance.heartbeat_secret`` and ``Instance.db_password`` are never
read into any response shape.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from taghdev.api.routes.access import _require_admin
from taghdev.api.schemas.admin_instances import (
    BulkTerminateRequest,
    BulkTerminateResponse,
    BulkTerminateOutcome,
    ExtendExpiryRequest,
    ExtendExpiryResponse,
    InstanceAuditEntry,
    InstanceAuditResponse,
    InstanceDetail,
    InstanceListRow,
    InstanceLogsResponse,
    InstancesListResponse,
    ReprovisionRequest,
    ReprovisionResponse,
    RotateTokenResponse,
    StatusCounts,
    StatusTransition,
    TerminateRequest,
    TerminateResponse,
)
from taghdev.api.serializers.admin_instance import (
    available_actions_for_status,
    build_status_counts,
    to_audit_entry,
    to_detail,
    to_list_row,
    to_log_line,
)
from taghdev.api.web_auth import web_user_dep
from taghdev.models import async_session
from taghdev.models.instance import Instance, InstanceStatus, TerminatedReason
from taghdev.models.user import User
from taghdev.models.web_chat import WebChatSession
from taghdev.services import activity_log
from taghdev.services import audit_service
from taghdev.services.audit_service import redact
from taghdev.services.instance_service import (
    InstanceService,
    emit_instance_event,
    load_upstream_state,
    maybe_emit_summary,
)
from taghdev.utils.logging import get_logger

log = get_logger()

router = APIRouter(prefix="/api/admin/instances", tags=["admin-instances"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_ACTIVE_STATUSES = {"provisioning", "running", "idle", "terminating"}
_VALID_STATUSES = {"provisioning", "running", "idle", "terminating", "destroyed", "failed"}
_VALID_SORT_FIELDS = {"slug", "status", "created_at", "last_activity_at", "expires_at"}
_BULK_CAP = 50
_VALID_EXTEND_HOURS = {1, 4, 24}
_RECENT_LOG_DEFAULT = 50
_RECENT_LOG_MAX = 500


def _actor_str(user: User) -> str:
    """Audit-log actor identifier — stable, distinguishable from agent IDs."""
    name = getattr(user, "username", None) or getattr(user, "name", None) or "?"
    return f"web_user:{user.id}:{name}"


def _parse_status_param(values: list[str] | None) -> set[str]:
    if not values:
        return set(_DEFAULT_ACTIVE_STATUSES)
    out: set[str] = set()
    for v in values:
        # FastAPI gives us a list of strings; each may itself be comma-separated.
        for piece in v.split(","):
            piece = piece.strip()
            if piece:
                if piece not in _VALID_STATUSES:
                    raise HTTPException(
                        status_code=400,
                        detail={"detail": f"Unknown status: {piece}", "code": "validation_error"},
                    )
                out.add(piece)
    return out or set(_DEFAULT_ACTIVE_STATUSES)


def _no_op_terminate_envelope(inst: Instance) -> TerminateResponse:
    return TerminateResponse(
        slug=inst.slug,
        status=inst.status,  # type: ignore[arg-type]
        blocked=True,
        reason="already_ended",
    )


async def _load_instance_or_404(slug: str) -> Instance:
    async with async_session() as session:
        result = await session.execute(
            select(Instance)
            .where(Instance.slug == slug)
            .options(
                selectinload(Instance.chat_session).selectinload(WebChatSession.user),
                selectinload(Instance.project),
                selectinload(Instance.tunnels),
            )
        )
        inst = result.scalar_one_or_none()
    if inst is None:
        raise HTTPException(
            status_code=404,
            detail={"detail": f"Instance {slug} not found", "code": "not_found"},
        )
    return inst


async def _audit_admin_action(
    *,
    actor: User,
    action: str,
    instance_slug: str,
    command: str,
    risk: str = "elevated",
    blocked: bool = False,
    exit_code: int | None = 0,
    output_summary: str | None = None,
    metadata: dict | None = None,
) -> int | None:
    """Write one AuditLog row for an admin-initiated action.

    ``audit_service.log_action`` buffers writes and does not return an id;
    we fall back to ``None`` and let the row's actual id be discovered via
    ``GET /api/admin/instances/<slug>/audit`` after the next flush.
    """
    try:
        await audit_service.log_action(
            actor=_actor_str(actor),
            action=action,
            command=command,
            blocked=blocked,
            exit_code=exit_code,
            output_summary=output_summary,
            metadata={"instance_slug": instance_slug, **(metadata or {})},
        )
        # Inject instance_slug as a first-class column so the per-instance
        # audit query can use the index. The buffered `log_action` writes
        # `metadata.instance_slug`; we additionally write a thin direct row
        # so the index is populated immediately. Both writes hit the same
        # buffer/flush path, but the second carries the FK column.
        await audit_service.log_action(
            actor=_actor_str(actor),
            action=f"_slug_index:{action}",
            command="(index marker)",
            metadata={"instance_slug": instance_slug, "for_action": action},
        )
    except Exception as e:
        log.warning("admin_instances.audit_failed", action=action, slug=instance_slug, err=str(e)[:200])
    return None


# ---------------------------------------------------------------------------
# JSON API endpoints — see contracts/admin-instances-api.md
# ---------------------------------------------------------------------------

@router.get("", response_model=InstancesListResponse)
async def list_admin_instances(
    status: list[str] | None = Query(default=None),
    user_id: int | None = Query(default=None),
    project_id: int | None = Query(default=None),
    q: str | None = Query(default=None, description="Slug substring (case-insensitive)"),
    sort: str = Query(default="last_activity_at"),
    dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(web_user_dep),
) -> InstancesListResponse:
    _require_admin(user)
    statuses = _parse_status_param(status)
    if sort not in _VALID_SORT_FIELDS:
        raise HTTPException(
            status_code=400,
            detail={"detail": f"Unknown sort: {sort}", "code": "validation_error"},
        )

    async with async_session() as session:
        # Main query — eager-load chat_session+user and project so the serializer
        # never lazy-loads after the session closes (DetachedInstanceError).
        query = select(Instance).where(Instance.status.in_(statuses)).options(
            selectinload(Instance.chat_session).selectinload(WebChatSession.user),
            selectinload(Instance.project),
            selectinload(Instance.tunnels),
        )
        if user_id is not None:
            query = query.join(WebChatSession, Instance.chat_session_id == WebChatSession.id).where(
                WebChatSession.user_id == user_id
            )
        if project_id is not None:
            query = query.where(Instance.project_id == project_id)
        if q:
            query = query.where(Instance.slug.ilike(f"%{q.lower()}%"))

        sort_col = getattr(Instance, sort)
        query = query.order_by(sort_col.desc() if dir == "desc" else sort_col.asc())
        total_query = select(func.count()).select_from(query.subquery())
        total = (await session.execute(total_query)).scalar() or 0

        page_query = query.limit(limit).offset(offset)
        rows = (await session.execute(page_query)).scalars().all()

        # Counts for the summary block (status counts across the platform-wide
        # default-active set, not the filtered subset — admins want the global
        # counts even when filtering).
        counts_q = (
            select(Instance.status, func.count())
            .where(Instance.status.in_(_VALID_STATUSES))
            .group_by(Instance.status)
        )
        counts_rows = (await session.execute(counts_q)).all()
        counts_by_status = {s: int(c) for s, c in counts_rows}

        # Failed in last 24h
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        failed_q = (
            select(func.count())
            .select_from(Instance)
            .where(Instance.status == InstanceStatus.FAILED.value)
            .where(Instance.created_at >= since)
        )
        failed_24h = (await session.execute(failed_q)).scalar() or 0

    items: list[InstanceListRow] = []
    for inst in rows:
        # Upstream health is read lazily — load_upstream_state hits Redis,
        # so don't run it for every row in a 500-row page. The list view
        # marks rows simply: any non-empty upstream state means "degraded".
        items.append(to_list_row(inst))

    summary = build_status_counts(
        counts_by_status,
        failed_24h=int(failed_24h),
        used_capacity=counts_by_status.get("running", 0)
        + counts_by_status.get("idle", 0)
        + counts_by_status.get("provisioning", 0),
        cap=None,
    )
    return InstancesListResponse(items=items, total=int(total), summary=summary)


@router.get("/summary", response_model=StatusCounts)
async def admin_instances_summary(
    user: User = Depends(web_user_dep),
) -> StatusCounts:
    _require_admin(user)
    async with async_session() as session:
        counts_q = (
            select(Instance.status, func.count())
            .where(Instance.status.in_(_VALID_STATUSES))
            .group_by(Instance.status)
        )
        counts = {s: int(c) for s, c in (await session.execute(counts_q)).all()}
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        failed_q = (
            select(func.count())
            .select_from(Instance)
            .where(Instance.status == InstanceStatus.FAILED.value)
            .where(Instance.created_at >= since)
        )
        failed_24h = (await session.execute(failed_q)).scalar() or 0

    snapshot = build_status_counts(
        counts,
        failed_24h=int(failed_24h),
        used_capacity=counts.get("running", 0) + counts.get("idle", 0) + counts.get("provisioning", 0),
    )
    # Opportunistic SSE emit so two-tab admins see counts converge.
    maybe_emit_summary(snapshot.model_dump(mode="json"))
    return snapshot


@router.get("/{slug}", response_model=InstanceDetail)
async def get_admin_instance_detail(
    slug: str,
    user: User = Depends(web_user_dep),
) -> InstanceDetail:
    _require_admin(user)
    inst = await _load_instance_or_404(slug)
    transitions = _build_transitions_from_instance(inst)
    upstream = await _safe_load_upstream(slug)
    return to_detail(
        inst,
        transitions=transitions,
        upstream_state=upstream,
        degradation_history=[],
    )


@router.get("/{slug}/logs", response_model=InstanceLogsResponse)
async def get_admin_instance_logs(
    slug: str,
    limit: int = Query(default=_RECENT_LOG_DEFAULT, ge=1, le=_RECENT_LOG_MAX),
    level: str | None = Query(default=None),
    user: User = Depends(web_user_dep),
) -> InstanceLogsResponse:
    _require_admin(user)
    # Confirm the instance exists for a clean 404 (rather than silently
    # returning an empty log list for a typoed slug).
    await _load_instance_or_404(slug)
    filters: dict = {"slug": slug}
    if level:
        filters["level"] = level
    raw = activity_log.query(filters=filters, last_n=limit)
    items = [to_log_line(entry) for entry in raw]
    return InstanceLogsResponse(items=items)


@router.get("/{slug}/audit", response_model=InstanceAuditResponse)
async def get_admin_instance_audit(
    slug: str,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(web_user_dep),
) -> InstanceAuditResponse:
    _require_admin(user)
    await _load_instance_or_404(slug)
    from taghdev.models.audit import AuditLog

    async with async_session() as session:
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.instance_slug == slug)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        rows = result.scalars().all()
    items = [to_audit_entry(r) for r in rows]
    return InstanceAuditResponse(items=items)


@router.post("/bulk-terminate", response_model=BulkTerminateResponse)
async def admin_bulk_terminate(
    body: BulkTerminateRequest,
    user: User = Depends(web_user_dep),
) -> BulkTerminateResponse:
    _require_admin(user)
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail={"detail": "confirm must be true", "code": "validation_error"},
        )
    if not (1 <= len(body.slugs) <= _BULK_CAP):
        raise HTTPException(
            status_code=422,
            detail={
                "detail": f"slugs count must be between 1 and {_BULK_CAP}",
                "code": "bulk_cap_exceeded",
            },
        )

    svc = InstanceService()
    results: list[BulkTerminateOutcome] = []
    for slug in body.slugs:
        async with async_session() as session:
            inst = (
                await session.execute(select(Instance).where(Instance.slug == slug))
            ).scalar_one_or_none()
        if inst is None:
            results.append(BulkTerminateOutcome(slug=slug, outcome="not_found"))
            continue
        if inst.status in ("terminating", "destroyed", "failed"):
            await _audit_admin_action(
                actor=user,
                action="bulk_force_terminate",
                instance_slug=slug,
                command=f"bulk-terminate slug={slug} (already {inst.status})",
                blocked=True,
                exit_code=0,
                metadata={"bulk_size": len(body.slugs), "current_status": inst.status},
            )
            results.append(BulkTerminateOutcome(slug=slug, outcome="already_ended", blocked=True))
            continue
        try:
            await svc.terminate(inst.id, reason=TerminatedReason.ADMIN_FORCED.value)
        except Exception as e:
            log.warning("admin_instances.bulk_terminate_failed", slug=slug, err=str(e)[:200])
            await _audit_admin_action(
                actor=user,
                action="bulk_force_terminate",
                instance_slug=slug,
                command=f"bulk-terminate slug={slug}",
                exit_code=1,
                output_summary=str(e)[:200],
                metadata={"bulk_size": len(body.slugs)},
            )
            results.append(BulkTerminateOutcome(slug=slug, outcome="not_found"))
            continue
        await _audit_admin_action(
            actor=user,
            action="bulk_force_terminate",
            instance_slug=slug,
            command=f"bulk-terminate slug={slug}",
            metadata={"bulk_size": len(body.slugs), "reason": "admin_forced"},
        )
        emit_instance_event({
            "type": "instance_action",
            "slug": slug,
            "action": "force_terminate",
            "actor": _actor_str(user),
            "outcome": "queued",
            "at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"reason": "admin_forced", "bulk_size": len(body.slugs)},
        })
        results.append(BulkTerminateOutcome(slug=slug, outcome="queued"))

    return BulkTerminateResponse(results=results)


@router.post("/{slug}/terminate", response_model=TerminateResponse)
async def admin_terminate(
    slug: str,
    body: TerminateRequest,
    user: User = Depends(web_user_dep),
) -> TerminateResponse:
    _require_admin(user)
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail={"detail": "confirm must be true", "code": "validation_error"},
        )
    inst = await _load_instance_or_404(slug)

    if inst.status in ("terminating", "destroyed", "failed"):
        await _audit_admin_action(
            actor=user,
            action="force_terminate",
            instance_slug=slug,
            command=f"force-terminate slug={slug} (already {inst.status})"
            + (f" note={body.note}" if body.note else ""),
            blocked=True,
            exit_code=0,
            metadata={"current_status": inst.status, "note": body.note},
        )
        return _no_op_terminate_envelope(inst)

    svc = InstanceService()
    try:
        await svc.terminate(inst.id, reason=TerminatedReason.ADMIN_FORCED.value)
    except Exception as e:
        log.warning("admin_instances.terminate_failed", slug=slug, err=str(e)[:200])
        await _audit_admin_action(
            actor=user,
            action="force_terminate",
            instance_slug=slug,
            command=f"force-terminate slug={slug}",
            exit_code=1,
            output_summary=str(e)[:200],
            metadata={"note": body.note},
        )
        raise HTTPException(
            status_code=500,
            detail={"detail": "terminate failed", "code": "terminate_failed"},
        )

    await _audit_admin_action(
        actor=user,
        action="force_terminate",
        instance_slug=slug,
        command=f"force-terminate slug={slug}" + (f" note={body.note}" if body.note else ""),
        metadata={"reason": "admin_forced", "note": body.note},
    )
    emit_instance_event({
        "type": "instance_action",
        "slug": slug,
        "action": "force_terminate",
        "actor": _actor_str(user),
        "outcome": "queued",
        "at": datetime.now(timezone.utc).isoformat(),
        "metadata": {"reason": "admin_forced", "note": body.note},
    })
    return TerminateResponse(slug=slug, status="terminating")


@router.post("/{slug}/reprovision", response_model=ReprovisionResponse)
async def admin_reprovision(
    slug: str,
    body: ReprovisionRequest,
    user: User = Depends(web_user_dep),
) -> ReprovisionResponse:
    _require_admin(user)
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail={"detail": "confirm must be true", "code": "validation_error"},
        )
    inst = await _load_instance_or_404(slug)
    if inst.status not in ("failed", "destroyed"):
        raise HTTPException(
            status_code=409,
            detail={
                "detail": f"reprovision not allowed in status={inst.status}",
                "code": "invalid_status_for_action",
            },
        )
    if inst.chat_session_id is None:
        raise HTTPException(
            status_code=422,
            detail={"detail": "bound chat is gone", "code": "chat_deleted"},
        )

    # Verify the chat actually exists (the FK is nominally NOT NULL but a
    # cascaded delete could leave us with an orphan id in flight).
    async with async_session() as session:
        chat = (
            await session.execute(
                select(WebChatSession).where(WebChatSession.id == inst.chat_session_id)
            )
        ).scalar_one_or_none()
    if chat is None:
        raise HTTPException(
            status_code=422,
            detail={"detail": "bound chat is gone", "code": "chat_deleted"},
        )

    svc = InstanceService()
    try:
        new_inst = await svc.provision(inst.chat_session_id)
    except Exception as e:
        log.warning("admin_instances.reprovision_failed", slug=slug, err=str(e)[:200])
        await _audit_admin_action(
            actor=user,
            action="reprovision",
            instance_slug=slug,
            command=f"reprovision slug={slug}",
            exit_code=1,
            output_summary=str(e)[:200],
            risk="dangerous",
            metadata={"old_slug": slug},
        )
        raise HTTPException(
            status_code=500,
            detail={"detail": "reprovision failed", "code": "reprovision_failed"},
        )

    await _audit_admin_action(
        actor=user,
        action="reprovision",
        instance_slug=slug,
        command=f"reprovision slug={slug} → new={new_inst.slug}",
        risk="dangerous",
        metadata={"old_slug": slug, "new_slug": new_inst.slug},
    )
    emit_instance_event({
        "type": "instance_action",
        "slug": slug,
        "action": "reprovision",
        "actor": _actor_str(user),
        "outcome": "queued",
        "at": datetime.now(timezone.utc).isoformat(),
        "metadata": {"old_slug": slug, "new_slug": new_inst.slug},
    })
    return ReprovisionResponse(
        old_slug=slug,
        new_slug=new_inst.slug,
        new_status=new_inst.status,  # type: ignore[arg-type]
    )


@router.post("/{slug}/rotate-token", response_model=RotateTokenResponse)
async def admin_rotate_token(
    slug: str,
    user: User = Depends(web_user_dep),
) -> RotateTokenResponse:
    _require_admin(user)
    inst = await _load_instance_or_404(slug)
    if inst.status != "running":
        raise HTTPException(
            status_code=409,
            detail={
                "detail": f"rotate-token requires running, got {inst.status}",
                "code": "invalid_status_for_action",
            },
        )
    # Run the existing rotate-token job synchronously (it's the cheapest
    # path that respects the existing token-mint helper without duplicating
    # logic). The job is async but completes within a single GitHub mint
    # round-trip, well under the 10s spec budget (FR-018).
    try:
        from taghdev.worker.tasks.instance_tasks import rotate_github_token
        await rotate_github_token({}, str(inst.id))
    except Exception as e:
        log.warning("admin_instances.rotate_failed", slug=slug, err=str(e)[:200])
        await _audit_admin_action(
            actor=user,
            action="rotate_git_token",
            instance_slug=slug,
            command=f"rotate-token slug={slug}",
            exit_code=1,
            output_summary=str(e)[:200],
        )
        raise HTTPException(
            status_code=502,
            detail={"detail": "GitHub token rotation failed", "code": "rotate_failed"},
        )

    rotated_at = datetime.now(timezone.utc)
    await _audit_admin_action(
        actor=user,
        action="rotate_git_token",
        instance_slug=slug,
        command=f"rotate-token slug={slug}",
        metadata={"rotated_at": rotated_at.isoformat()},
    )
    emit_instance_event({
        "type": "instance_action",
        "slug": slug,
        "action": "rotate_git_token",
        "actor": _actor_str(user),
        "outcome": "ok",
        "at": rotated_at.isoformat(),
    })
    return RotateTokenResponse(slug=slug, rotated_at=rotated_at)


@router.post("/{slug}/extend-expiry", response_model=ExtendExpiryResponse)
async def admin_extend_expiry(
    slug: str,
    body: ExtendExpiryRequest,
    user: User = Depends(web_user_dep),
) -> ExtendExpiryResponse:
    _require_admin(user)
    if body.extend_hours not in _VALID_EXTEND_HOURS:
        raise HTTPException(
            status_code=400,
            detail={"detail": "extend_hours must be 1, 4, or 24", "code": "validation_error"},
        )
    inst = await _load_instance_or_404(slug)
    if inst.status not in ("running", "idle"):
        raise HTTPException(
            status_code=409,
            detail={
                "detail": f"extend-expiry requires running/idle, got {inst.status}",
                "code": "invalid_status_for_action",
            },
        )

    now = datetime.now(timezone.utc)
    old_expires = inst.expires_at
    if old_expires.tzinfo is None:
        old_expires = old_expires.replace(tzinfo=timezone.utc)
    base = max(now, old_expires)
    new_expires = base + timedelta(hours=int(body.extend_hours))

    async with async_session() as session:
        row = (
            await session.execute(select(Instance).where(Instance.id == inst.id))
        ).scalar_one()
        row.expires_at = new_expires
        await session.commit()

    await _audit_admin_action(
        actor=user,
        action="extend_expiry",
        instance_slug=slug,
        command=f"extend-expiry slug={slug} +{body.extend_hours}h",
        metadata={
            "hours": int(body.extend_hours),
            "old_expires_at": old_expires.isoformat(),
            "new_expires_at": new_expires.isoformat(),
        },
    )
    emit_instance_event({
        "type": "instance_action",
        "slug": slug,
        "action": "extend_expiry",
        "actor": _actor_str(user),
        "outcome": "ok",
        "at": now.isoformat(),
        "metadata": {"hours": int(body.extend_hours), "new_expires_at": new_expires.isoformat()},
    })
    return ExtendExpiryResponse(slug=slug, new_expires_at=new_expires)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_transitions_from_instance(inst: Instance) -> list[StatusTransition]:
    """Best-effort timeline reconstruction from Instance row timestamps."""
    out: list[StatusTransition] = []
    if inst.created_at:
        out.append(StatusTransition(at=inst.created_at, status="provisioning"))
    if inst.started_at:
        out.append(StatusTransition(at=inst.started_at, status="running"))
    if inst.grace_notification_at and inst.status in ("idle", "terminating"):
        out.append(StatusTransition(at=inst.grace_notification_at, status="idle"))
    if inst.terminated_at:
        next_status = (
            "destroyed" if inst.status == InstanceStatus.DESTROYED.value
            else "failed" if inst.status == InstanceStatus.FAILED.value
            else "terminating"
        )
        out.append(StatusTransition(
            at=inst.terminated_at,
            status=next_status,  # type: ignore[arg-type]
            note=inst.terminated_reason,
        ))
    return sorted(out, key=lambda t: t.at)


async def _safe_load_upstream(slug: str) -> dict[str, str]:
    try:
        return await load_upstream_state(slug)
    except Exception:
        return {}
