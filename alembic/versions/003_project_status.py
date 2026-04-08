"""Add status column to projects for link/unlink lifecycle

Revision ID: 003
Revises: 002
Create Date: 2026-04-08
"""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("status", sa.String(20), server_default="active", nullable=False))


def downgrade():
    op.drop_column("projects", "status")
