"""Pydantic response shapes for the admin Instances section (spec 003).

These are derived view models — they do not correspond to DB tables.
The shapes are pinned by ``specs/003-admin-instance-mgmt/data-model.md``
§3 and the ``contracts/admin-instances-api.md`` document; reviewers
verify endpoint responses against them.

Principle IV — credential redaction: ``heartbeat_secret`` and
``db_password`` are NOT defined in any schema here. They cannot be
serialized by accident; the regression guard test in
``tests/unit/test_admin_instance_serializer.py`` enforces this.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Atomic blocks
# ---------------------------------------------------------------------------

class UserRef(BaseModel):
    id: int | None
    name: str
    deleted: bool = False


class ProjectRef(BaseModel):
    id: int | None
    name: str
    deleted: bool = False


class ChatRef(BaseModel):
    id: int | None
    deleted: bool = False
    link: str | None = None


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------

InstanceStatusLiteral = Literal[
    "provisioning", "running", "idle", "terminating", "destroyed", "failed"
]
UpstreamHealthLiteral = Literal["live", "degraded", "unreachable"]


class InstanceListRow(BaseModel):
    slug: str
    status: InstanceStatusLiteral
    status_age_seconds: int
    user: UserRef
    project: ProjectRef
    preview_url: str | None = None
    created_at: datetime
    last_activity_at: datetime | None = None
    expires_at: datetime | None = None
    upstream_health: UpstreamHealthLiteral | None = None


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------

class CapacityRef(BaseModel):
    used: int
    cap: int | None = None


class StatusCounts(BaseModel):
    running: int = 0
    idle: int = 0
    provisioning: int = 0
    terminating: int = 0
    failed_24h: int = 0
    total_active: int = 0
    capacity: CapacityRef


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------

class StatusTransition(BaseModel):
    at: datetime
    status: InstanceStatusLiteral
    note: str | None = None


class DegradationEvent(BaseModel):
    at: datetime
    capability: str
    upstream: str
    health: UpstreamHealthLiteral


class TunnelInfo(BaseModel):
    url: str | None = None
    health: UpstreamHealthLiteral | None = None
    degradation_history: list[DegradationEvent] = []


class FailureInfo(BaseModel):
    code: str
    message: str | None = None


AvailableActionLiteral = Literal[
    "force_terminate",
    "reprovision",
    "rotate_git_token",
    "extend_expiry",
    "open_preview",
    "open_in_chat",
]


class InstanceDetail(BaseModel):
    # Mirrors InstanceListRow plus diagnostic fields. heartbeat_secret /
    # db_password DELIBERATELY ABSENT — they MUST never be serialized
    # to admin-facing responses (Principle IV).
    slug: str
    status: InstanceStatusLiteral
    status_age_seconds: int
    user: UserRef
    project: ProjectRef
    chat: ChatRef
    preview_url: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    expires_at: datetime | None = None
    grace_notification_at: datetime | None = None
    terminated_at: datetime | None = None
    terminated_reason: str | None = None
    compose_project: str | None = None
    workspace_path: str | None = None
    session_branch: str | None = None
    image_digest: str | None = None
    resource_profile: str | None = None
    transitions: list[StatusTransition] = []
    tunnel: TunnelInfo
    failure: FailureInfo | None = None
    available_actions: list[AvailableActionLiteral] = []


# ---------------------------------------------------------------------------
# Logs and audit
# ---------------------------------------------------------------------------

class InstanceLogLine(BaseModel):
    ts: datetime
    level: str = "info"
    message: str
    context: dict = {}


class InstanceAuditEntry(BaseModel):
    actor: str
    action: str
    command: str
    exit_code: int | None = None
    output_summary: str | None = None
    risk_level: str = "normal"
    blocked: bool = False
    metadata: dict | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Page-level wrappers
# ---------------------------------------------------------------------------

class InstancesListResponse(BaseModel):
    items: list[InstanceListRow]
    total: int
    summary: StatusCounts


class InstanceLogsResponse(BaseModel):
    items: list[InstanceLogLine]


class InstanceAuditResponse(BaseModel):
    items: list[InstanceAuditEntry]


# ---------------------------------------------------------------------------
# Action request / response shapes
# ---------------------------------------------------------------------------

class TerminateRequest(BaseModel):
    confirm: bool
    note: str | None = None


class TerminateResponse(BaseModel):
    slug: str
    status: InstanceStatusLiteral
    audit_id: int | None = None
    blocked: bool = False
    reason: str | None = None


class BulkTerminateRequest(BaseModel):
    slugs: list[str]
    confirm: bool


class BulkTerminateOutcome(BaseModel):
    slug: str
    outcome: Literal["queued", "already_ended", "not_found"]
    audit_id: int | None = None
    blocked: bool = False


class BulkTerminateResponse(BaseModel):
    results: list[BulkTerminateOutcome]


class ReprovisionRequest(BaseModel):
    confirm: bool


class ReprovisionResponse(BaseModel):
    old_slug: str
    new_slug: str
    new_status: InstanceStatusLiteral
    audit_id: int | None = None


class RotateTokenResponse(BaseModel):
    slug: str
    rotated_at: datetime
    audit_id: int | None = None


class ExtendExpiryRequest(BaseModel):
    extend_hours: Literal[1, 4, 24]


class ExtendExpiryResponse(BaseModel):
    slug: str
    new_expires_at: datetime
    audit_id: int | None = None
