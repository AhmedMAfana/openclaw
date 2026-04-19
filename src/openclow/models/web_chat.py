from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Boolean, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from openclow.models.base import Base


class WebChatSession(Base):
    """Conversation session — each user chat is a separate session with its own history."""
    __tablename__ = "web_chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), default="New Chat")
    mode: Mapped[str] = mapped_column(String(20), default="quick")  # "quick" | "plan"
    git_mode: Mapped[str] = mapped_column(String(20), default="branch_per_task")  # "branch_per_task" | "direct_commit" | "session_branch"
    session_branch_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_message_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", lazy="joined")
    project = relationship("Project", lazy="joined", foreign_keys=[project_id])
    messages = relationship("WebChatMessage", back_populates="session", cascade="all, delete-orphan")


class WebChatMessage(Base):
    """Individual message in a session — persisted for multi-device history."""
    __tablename__ = "web_chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("web_chat_sessions.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # "user" | "assistant" | "plan"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    plan_file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # for role="plan"
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session = relationship("WebChatSession", back_populates="messages")
    user = relationship("User", lazy="joined")


class Plan(Base):
    """Proposed plan document — saved to disk and linked to the message."""
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("web_chat_sessions.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("web_chat_messages.id"), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)  # absolute path on disk
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending_review")  # pending_review | approved | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session = relationship("WebChatSession")
    user = relationship("User")
    message = relationship("WebChatMessage")
