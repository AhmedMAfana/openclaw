"""Per-instance Cloudflare named tunnel.

Spec: specs/001-per-chat-instances/data-model.md §2
Migration: alembic/versions/011_instance_tables.py

Constitution Principle IV: `credentials_secret` stores the Docker secret
NAME, never the credential JSON. Constitution Principle VI: exactly one
active tunnel per instance, enforced by partial unique index
uq_instance_tunnels_one_active.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from openclow.models.base import Base


class TunnelStatus(str, enum.Enum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    ROTATING = "rotating"
    DESTROYED = "destroyed"


class InstanceTunnel(Base):
    __tablename__ = "instance_tunnels"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    cf_tunnel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cf_tunnel_name: Mapped[str] = mapped_column(
        String(40), nullable=False, unique=True
    )
    web_hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    hmr_hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    ide_hostname: Mapped[str | None] = mapped_column(String(255))
    credentials_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TunnelStatus.PROVISIONING.value
    )
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    instance = relationship("Instance", back_populates="tunnels")
