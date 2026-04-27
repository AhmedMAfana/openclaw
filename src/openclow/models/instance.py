"""Per-chat isolated instance — one dev environment bound to one chat.

Spec: specs/001-per-chat-instances/data-model.md §1
Migration: alembic/versions/011_instance_tables.py

Invariants enforced at the DB layer (do not duplicate in application code):
  * one active instance per chat (partial unique index uq_instances_active_per_chat)
  * status is a closed enum (CHECK constraint ck_instances_status)
  * slug format `inst-<14 hex>` (CHECK constraint ck_instances_slug_format,
    56-bit entropy per FR-018a)
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from openclow.models.base import Base


class InstanceStatus(str, enum.Enum):
    PROVISIONING = "provisioning"
    RUNNING = "running"
    IDLE = "idle"
    TERMINATING = "terminating"
    DESTROYED = "destroyed"
    FAILED = "failed"

    @classmethod
    def active(cls) -> set[str]:
        """Statuses that count as 'active' for the per-user cap."""
        return {cls.PROVISIONING, cls.RUNNING, cls.IDLE, cls.TERMINATING}


class FailureCode(str, enum.Enum):
    IMAGE_BUILD = "image_build"
    COMPOSE_UP = "compose_up"
    PROJCTL_UP = "projctl_up"
    TUNNEL_PROVISION = "tunnel_provision"
    DNS = "dns"
    HEALTH_CHECK = "health_check"
    OOM = "oom"
    STORAGE_FULL = "storage_full"
    ORCHESTRATOR_CRASH = "orchestrator_crash"
    UNKNOWN = "unknown"


class TerminatedReason(str, enum.Enum):
    IDLE_24H = "idle_24h"
    USER_REQUEST = "user_request"
    FAILED = "failed"
    PROJECT_DELETED = "project_deleted"
    CHAT_DELETED = "chat_deleted"
    ADMIN_FORCED = "admin_forced"


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    chat_session_id: Mapped[int] = mapped_column(
        ForeignKey("web_chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=InstanceStatus.PROVISIONING.value
    )
    compose_project: Mapped[str] = mapped_column(String(30), nullable=False)
    workspace_path: Mapped[str] = mapped_column(String(255), nullable=False)
    session_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    image_digest: Mapped[str | None] = mapped_column(String(255))
    resource_profile: Mapped[str] = mapped_column(
        String(20), nullable=False, default="standard"
    )
    heartbeat_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    db_password: Mapped[str] = mapped_column(String(64), nullable=False)
    per_user_count_at_provision: Mapped[int] = mapped_column(
        SmallInteger, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    grace_notification_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminated_reason: Mapped[str | None] = mapped_column(String(30))
    failure_code: Mapped[str | None] = mapped_column(String(30))
    failure_message: Mapped[str | None] = mapped_column(Text)

    chat_session = relationship(
        "WebChatSession",
        foreign_keys=[chat_session_id],
        back_populates="instance_bound",
    )
    project = relationship("Project", lazy="joined")
    tunnels = relationship(
        "InstanceTunnel",
        back_populates="instance",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
