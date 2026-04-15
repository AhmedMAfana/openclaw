"""Add is_admin and default_project_id to users

Revision ID: c0875d86c2ab
Revises: 004
Create Date: 2026-04-13

"""
from alembic import op
import sqlalchemy as sa


revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "users",
        sa.Column(
            "default_project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id"),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("users", "default_project_id")
    op.drop_column("users", "is_admin")
