"""Add chat_provider_type to tasks so orchestrator knows which provider created the task

Revision ID: 004
Revises: 003
Create Date: 2026-04-09
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "tasks",
        sa.Column("chat_provider_type", sa.String(50), server_default="telegram", nullable=False),
    )


def downgrade():
    op.drop_column("tasks", "chat_provider_type")
