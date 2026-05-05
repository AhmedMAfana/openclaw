"""Audit log model — immutable record of every AI agent action.

Every shell command, file edit, Docker operation, and git command
executed by an agent gets logged here. This is the "black box recorder"
for understanding what Claude did and why.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from taghdev.models.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Who did it
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    # e.g., "doctor", "coder", "bootstrap", "orchestrator"

    # What was done
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    # e.g., "bash", "docker", "file_edit", "git_push", "git_force_push"

    # The actual command or operation
    command: Mapped[str] = mapped_column(Text, nullable=False)
    # e.g., "docker compose up -d --build", "edit /app/Dockerfile"

    # Where it happened
    workspace: Mapped[str | None] = mapped_column(String(500))
    project_name: Mapped[str | None] = mapped_column(String(255))
    # Per-chat instance slug (``inst-<14 hex>``). Nullable for rows written
    # before migration 013 and for non-instance-scoped audit events (e.g.
    # orchestrator maintenance). Principle I: MCP tool calls MUST populate
    # this so a post-hoc auditor can prove one-instance-per-task.
    instance_slug: Mapped[str | None] = mapped_column(String(20), index=True)

    # Result
    exit_code: Mapped[int | None] = mapped_column(Integer)
    output_summary: Mapped[str | None] = mapped_column(Text)
    # First 2000 chars of output — enough to diagnose, not enough to bloat DB

    # Risk level
    risk_level: Mapped[str] = mapped_column(String(20), default="normal")
    # "normal", "elevated", "dangerous"
    # elevated = docker operations, git push
    # dangerous = docker rm, git push --force, rm -rf

    # Was it blocked by the allowlist?
    blocked: Mapped[bool] = mapped_column(default=False)

    # Extra context
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    # task_id, container_name, branch, etc.

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False,
    )
