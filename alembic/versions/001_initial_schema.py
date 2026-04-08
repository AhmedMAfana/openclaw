"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-04-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "platform_config",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.UniqueConstraint("category", "key", name="uq_platform_config_category_key"),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("chat_provider_type", sa.String(50), default="telegram"),
        sa.Column("chat_provider_uid", sa.String(255), unique=True, nullable=False),
        sa.Column("username", sa.String(255)),
        sa.Column("is_allowed", sa.Boolean, default=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100), unique=True, nullable=False),
        sa.Column("github_repo", sa.String(255), nullable=False),
        sa.Column("default_branch", sa.String(100), default="main"),
        sa.Column("description", sa.Text),
        sa.Column("tech_stack", sa.String(255)),
        sa.Column("agent_system_prompt", sa.Text),
        sa.Column("force_fresh_install", sa.Boolean, default=False),
        sa.Column("setup_commands", sa.Text),
        sa.Column("is_dockerized", sa.Boolean, default=True),
        sa.Column("docker_compose_file", sa.String(255)),
        sa.Column("app_container_name", sa.String(255)),
        sa.Column("app_port", sa.Integer),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("status", sa.String(50), nullable=False, default="pending"),
        sa.Column("branch_name", sa.String(255)),
        sa.Column("pr_url", sa.String(500)),
        sa.Column("pr_number", sa.Integer),
        sa.Column("arq_job_id", sa.String(255)),
        sa.Column("chat_id", sa.String(255), nullable=False),
        sa.Column("chat_message_id", sa.String(255)),
        sa.Column("error_message", sa.Text),
        sa.Column("agent_turns", sa.Integer),
        sa.Column("duration_seconds", sa.Integer),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "task_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("agent", sa.String(50), default="system"),
        sa.Column("level", sa.String(20), default="info"),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("metadata", JSONB),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("task_logs")
    op.drop_table("tasks")
    op.drop_table("projects")
    op.drop_table("users")
    op.drop_table("platform_config")
