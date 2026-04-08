"""Add audit_logs table for tracking all AI agent actions

Revision ID: 002
Revises: 001
Create Date: 2026-04-08
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("command", sa.Text, nullable=False),
        sa.Column("workspace", sa.String(500)),
        sa.Column("project_name", sa.String(255)),
        sa.Column("exit_code", sa.Integer),
        sa.Column("output_summary", sa.Text),
        sa.Column("risk_level", sa.String(20), server_default="normal"),
        sa.Column("blocked", sa.Boolean, server_default=sa.text("false")),
        sa.Column("metadata", JSONB),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )

    # Index for querying by actor, project, risk level
    op.create_index("ix_audit_logs_actor", "audit_logs", ["actor"])
    op.create_index("ix_audit_logs_project_name", "audit_logs", ["project_name"])
    op.create_index("ix_audit_logs_risk_level", "audit_logs", ["risk_level"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_blocked", "audit_logs", ["blocked"],
                     postgresql_where=sa.text("blocked = true"))


def downgrade():
    op.drop_table("audit_logs")
