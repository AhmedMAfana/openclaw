"""Serialization helpers for admin Instance responses (spec 003).

Two responsibilities:

1. Build the derived view shapes (``InstanceListRow``, ``InstanceDetail``,
   ``InstanceLogLine``) without ever touching ``heartbeat_secret`` or
   ``db_password`` (Principle IV — credential redaction).
2. Wrap any free-text content destined for the admin UI through
   ``audit_service.redact()`` so a stray bearer token or AWS key in a
   log line never reaches the dashboard.

The single source of truth for available actions per status is
``available_actions_for_status()`` here; the API and the templates both
call it so the front-end never has to duplicate state-machine logic.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from openclow.api.schemas.admin_instances import (
    AvailableActionLiteral,
    ChatRef,
    DegradationEvent,
    FailureInfo,
    InstanceAuditEntry,
    InstanceDetail,
    InstanceListRow,
    InstanceLogLine,
    ProjectRef,
    StatusCounts,
    StatusTransition,
    TunnelInfo,
    UserRef,
)
from openclow.models.instance import Instance, InstanceStatus
from openclow.services.audit_service import redact


# ---------------------------------------------------------------------------
# Action allowlists per status
# ---------------------------------------------------------------------------

def available_actions_for_status(
    status: str, has_chat: bool, has_preview_url: bool
) -> list[AvailableActionLiteral]:
    """Single source of truth for which buttons render in which state."""
    out: list[AvailableActionLiteral] = []
    if status in ("provisioning", "running", "idle"):
        out.append("force_terminate")
    elif status in ("failed", "destroyed"):
        if has_chat:
            out.append("reprovision")
        out.append("force_terminate")  # idempotent no-op, but keep it visible
    if status == "running":
        out.append("rotate_git_token")
    if status in ("running", "idle"):
        out.append("extend_expiry")
    if has_preview_url and status in ("running", "idle"):
        out.append("open_preview")
    if has_chat:
        out.append("open_in_chat")
    return out


# ---------------------------------------------------------------------------
# Atomic refs
# ---------------------------------------------------------------------------

def _user_ref(chat_session: Any | None) -> UserRef:
    if chat_session is None:
        return UserRef(id=None, name="(unknown)", deleted=True)
    user = getattr(chat_session, "user", None)
    if user is None:
        return UserRef(id=None, name="(unknown)", deleted=True)
    name = getattr(user, "username", None) or getattr(user, "name", None) or str(getattr(user, "id", "?"))
    return UserRef(id=getattr(user, "id", None), name=name, deleted=False)


def _project_ref(project: Any | None) -> ProjectRef:
    if project is None:
        return ProjectRef(id=None, name="(deleted project)", deleted=True)
    return ProjectRef(id=getattr(project, "id", None), name=getattr(project, "name", "?"), deleted=False)


def _chat_ref(chat_session: Any | None) -> ChatRef:
    if chat_session is None:
        return ChatRef(id=None, deleted=True, link=None)
    cid = getattr(chat_session, "id", None)
    return ChatRef(id=cid, deleted=False, link=f"/chat?session={cid}" if cid else None)


def _preview_url(inst: Instance) -> str | None:
    for tunnel in getattr(inst, "tunnels", []) or []:
        status = getattr(tunnel, "status", None)
        if status and status != "destroyed":
            host = getattr(tunnel, "web_hostname", None)
            if host:
                return f"https://{host}"
    return None


def _status_age_seconds(inst: Instance) -> int:
    # Use the most recent status-relevant timestamp we've got.
    candidates = [
        getattr(inst, "terminated_at", None),
        getattr(inst, "started_at", None),
        getattr(inst, "created_at", None),
    ]
    ts = next((c for c in candidates if c is not None), None)
    if ts is None:
        return 0
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0, int((now - ts).total_seconds()))


# ---------------------------------------------------------------------------
# List + detail builders
# ---------------------------------------------------------------------------

def to_list_row(inst: Instance, *, upstream_health: str | None = None) -> InstanceListRow:
    """Build the row shape for ``GET /api/admin/instances``.

    NEVER reads or returns ``heartbeat_secret``/``db_password``.
    """
    return InstanceListRow(
        slug=inst.slug,
        status=inst.status,  # type: ignore[arg-type]
        status_age_seconds=_status_age_seconds(inst),
        user=_user_ref(getattr(inst, "chat_session", None)),
        project=_project_ref(getattr(inst, "project", None)),
        preview_url=_preview_url(inst),
        created_at=inst.created_at,
        last_activity_at=inst.last_activity_at,
        expires_at=inst.expires_at,
        upstream_health=upstream_health,  # type: ignore[arg-type]
    )


def to_detail(
    inst: Instance,
    *,
    transitions: Iterable[StatusTransition] = (),
    upstream_state: dict[str, str] | None = None,
    degradation_history: Iterable[DegradationEvent] = (),
) -> InstanceDetail:
    """Build the full detail shape.

    Non-secret diagnostic fields (``compose_project``, ``workspace_path``,
    ``session_branch``, ``image_digest``, ``resource_profile``) are surfaced
    verbatim. Secrets are not even read.
    """
    chat_session = getattr(inst, "chat_session", None)
    has_chat = chat_session is not None
    preview = _preview_url(inst)
    # Map upstream-state dict to a single overall health signal — degraded if
    # any capability is in a non-empty failure state, else live if known.
    overall_health: str | None = None
    if upstream_state:
        overall_health = "degraded"
    elif preview:
        overall_health = "live"

    failure: FailureInfo | None = None
    if inst.status == InstanceStatus.FAILED.value and getattr(inst, "failure_code", None):
        failure = FailureInfo(
            code=inst.failure_code,
            message=redact(inst.failure_message) if inst.failure_message else None,
        )

    return InstanceDetail(
        slug=inst.slug,
        status=inst.status,  # type: ignore[arg-type]
        status_age_seconds=_status_age_seconds(inst),
        user=_user_ref(chat_session),
        project=_project_ref(getattr(inst, "project", None)),
        chat=_chat_ref(chat_session),
        preview_url=preview,
        created_at=inst.created_at,
        started_at=inst.started_at,
        last_activity_at=inst.last_activity_at,
        expires_at=inst.expires_at,
        grace_notification_at=inst.grace_notification_at,
        terminated_at=inst.terminated_at,
        terminated_reason=inst.terminated_reason,
        compose_project=inst.compose_project,
        workspace_path=inst.workspace_path,
        session_branch=inst.session_branch,
        image_digest=inst.image_digest,
        resource_profile=inst.resource_profile,
        transitions=list(transitions),
        tunnel=TunnelInfo(
            url=preview,
            health=overall_health,  # type: ignore[arg-type]
            degradation_history=list(degradation_history),
        ),
        failure=failure,
        available_actions=available_actions_for_status(
            inst.status, has_chat=has_chat, has_preview_url=bool(preview)
        ),
    )


# ---------------------------------------------------------------------------
# Log lines + audit entries
# ---------------------------------------------------------------------------

def to_log_line(entry: dict[str, Any]) -> InstanceLogLine:
    """Build one ``InstanceLogLine`` from an activity_log JSONL entry.

    Both the visible ``message`` and the bag of ``context`` values are routed
    through ``audit_service.redact()`` so secrets in a structured log payload
    don't leak via the admin UI.
    """
    ts = entry.get("ts")
    if isinstance(ts, (int, float)):
        when = datetime.fromtimestamp(ts, tz=timezone.utc)
    elif isinstance(ts, str):
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            when = datetime.now(timezone.utc)
    else:
        when = datetime.now(timezone.utc)

    raw_message = entry.get("message") or entry.get("event") or entry.get("type") or ""
    if not isinstance(raw_message, str):
        raw_message = str(raw_message)
    message = redact(raw_message)

    # Carry every non-control key as context so admins can debug, but route
    # values through the redactor first. JSON-encode + redact + decode is the
    # cheapest way to mask secrets buried inside nested dicts/lists without
    # losing structure. Strings stay strings; ints/bools/dicts re-emerge as
    # parsed JSON.
    context: dict[str, Any] = {}
    for key, value in entry.items():
        if key in ("ts", "type", "message", "event", "level", "slug"):
            continue
        try:
            encoded = json.dumps(value, default=str)
            context[key] = json.loads(redact(encoded))
        except Exception:
            context[key] = redact(str(value))

    level = entry.get("level") or "info"
    if not isinstance(level, str):
        level = "info"

    return InstanceLogLine(ts=when, level=level, message=message, context=context)


def to_audit_entry(row: Any) -> InstanceAuditEntry:
    """Build one ``InstanceAuditEntry`` from an ``AuditLog`` row or dict.

    ``command`` and ``output_summary`` are redacted so secret leakage via the
    admin audit panel is impossible (Principle IV).
    """
    g = (lambda k, default=None: row.get(k, default)) if isinstance(row, dict) else (lambda k, default=None: getattr(row, k, default))
    metadata = g("metadata_") if g("metadata_") is not None else g("metadata")
    return InstanceAuditEntry(
        actor=g("actor", "?") or "?",
        action=g("action", "?") or "?",
        command=redact(g("command") or ""),
        exit_code=g("exit_code"),
        output_summary=redact(g("output_summary")) if g("output_summary") else None,
        risk_level=g("risk_level", "normal") or "normal",
        blocked=bool(g("blocked", False)),
        metadata=metadata,
        created_at=g("created_at"),
    )


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------

def build_status_counts(
    counts_by_status: dict[str, int],
    *,
    failed_24h: int = 0,
    used_capacity: int = 0,
    cap: int | None = None,
) -> StatusCounts:
    return StatusCounts(
        running=counts_by_status.get("running", 0),
        idle=counts_by_status.get("idle", 0),
        provisioning=counts_by_status.get("provisioning", 0),
        terminating=counts_by_status.get("terminating", 0),
        failed_24h=failed_24h,
        total_active=sum(
            counts_by_status.get(s, 0)
            for s in ("provisioning", "running", "idle", "terminating")
        ),
        capacity={"used": used_capacity, "cap": cap},  # type: ignore[arg-type]
    )
