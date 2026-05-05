from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from openclow.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_provider_type: Mapped[str] = mapped_column(String(50), default="telegram")
    chat_provider_uid: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    is_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    default_project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    web_password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)  # bcrypt hash for web login
    git_token: Mapped[str | None] = mapped_column(String(512), nullable=True)  # personal GitHub PAT
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
