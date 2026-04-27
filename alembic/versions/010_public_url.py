"""Add public_url and tunnel_enabled to projects.

For host-mode deployments on a real VPS with nginx + domain, the public URL is
already known — cloudflared tunneling is redundant. `public_url` stores the
owned domain (e.g. https://tagh.example.com) and `tunnel_enabled` toggles
the tunnel pipeline off when the domain is authoritative.

Revision ID: 010
Revises: 009
Create Date: 2026-04-20
"""
from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("public_url", sa.String(500), nullable=True))
    op.add_column(
        "projects",
        sa.Column("tunnel_enabled", sa.Boolean(),
                  server_default=sa.text("true"), nullable=False),
    )


def downgrade():
    op.drop_column("projects", "tunnel_enabled")
    op.drop_column("projects", "public_url")
