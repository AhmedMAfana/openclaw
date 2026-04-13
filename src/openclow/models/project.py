from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from openclow.models.base import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    github_repo: Mapped[str] = mapped_column(String(255), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(100), default="main")
    description: Mapped[str | None] = mapped_column(Text)
    tech_stack: Mapped[str | None] = mapped_column(String(255))
    agent_system_prompt: Mapped[str | None] = mapped_column(Text)
    force_fresh_install: Mapped[bool] = mapped_column(Boolean, default=False)
    setup_commands: Mapped[str | None] = mapped_column(Text)
    # Docker-based projects
    is_dockerized: Mapped[bool] = mapped_column(Boolean, default=True)
    docker_compose_file: Mapped[str | None] = mapped_column(String(255), default="docker-compose.yml")
    app_container_name: Mapped[str | None] = mapped_column(String(255))  # e.g. "app" or "php"
    app_port: Mapped[int | None] = mapped_column(Integer)  # e.g. 8000
    # Lifecycle: bootstrapping → active (success) or failed (error); inactive = unlinked
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
