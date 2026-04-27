"""Add user_project_access table for per-user project RBAC.

Revision ID: 007
Revises: 006
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_project_access",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("project_id", sa.Integer, nullable=False),
        sa.Column("role", sa.String(20), server_default="developer", nullable=False),
        sa.Column("granted_by", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "project_id", name="uq_user_project_access"),
    )
    op.create_index("ix_upa_user_id", "user_project_access", ["user_id"])
    op.create_index("ix_upa_project_id", "user_project_access", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_upa_project_id", table_name="user_project_access")
    op.drop_index("ix_upa_user_id", table_name="user_project_access")
    op.drop_table("user_project_access")
