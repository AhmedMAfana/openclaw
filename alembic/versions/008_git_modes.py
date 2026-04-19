"""Add git_mode to tasks and web_chat_sessions for Direct Commit / Session Branch modes.

Revision ID: 008
Revises: 007
Create Date: 2026-04-18
"""
from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade():
    # tasks.git_mode
    op.add_column(
        "tasks",
        sa.Column("git_mode", sa.String(20), server_default="branch_per_task", nullable=False),
    )

    # web_chat_sessions.git_mode
    op.add_column(
        "web_chat_sessions",
        sa.Column("git_mode", sa.String(20), server_default="branch_per_task", nullable=False),
    )

    # web_chat_sessions.session_branch_name
    op.add_column(
        "web_chat_sessions",
        sa.Column("session_branch_name", sa.String(255), nullable=True),
    )


def downgrade():
    op.drop_column("web_chat_sessions", "session_branch_name")
    op.drop_column("web_chat_sessions", "git_mode")
    op.drop_column("tasks", "git_mode")
