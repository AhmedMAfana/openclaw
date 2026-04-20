"""Add host-mode columns to projects: mode, project_dir, install_guide_path,
start_command, stop_command, health_url, process_manager, auto_clone.

Sibling path to Docker mode — existing rows default to mode="docker" and keep
their current behavior. Host-mode projects set mode="host" and leave
docker_compose_file/app_container_name NULL.

Revision ID: 009
Revises: 008
Create Date: 2026-04-19
"""
from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "projects",
        sa.Column("mode", sa.String(10), server_default="docker", nullable=False),
    )
    op.add_column("projects", sa.Column("project_dir", sa.String(500), nullable=True))
    op.add_column("projects", sa.Column("install_guide_path", sa.String(255), nullable=True))
    op.add_column("projects", sa.Column("start_command", sa.String(500), nullable=True))
    op.add_column("projects", sa.Column("stop_command", sa.String(500), nullable=True))
    op.add_column("projects", sa.Column("health_url", sa.String(255), nullable=True))
    op.add_column("projects", sa.Column("process_manager", sa.String(50), nullable=True))
    op.add_column(
        "projects",
        sa.Column("auto_clone", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade():
    op.drop_column("projects", "auto_clone")
    op.drop_column("projects", "process_manager")
    op.drop_column("projects", "health_url")
    op.drop_column("projects", "stop_command")
    op.drop_column("projects", "start_command")
    op.drop_column("projects", "install_guide_path")
    op.drop_column("projects", "project_dir")
    op.drop_column("projects", "mode")
