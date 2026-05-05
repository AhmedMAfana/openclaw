"""Add git_token and web identity fields to users.

Per-user personal git token (GitHub PAT) stored encrypted-at-rest
as a nullable column. Also adds web_username as a login-friendly
alias separate from the existing `username` display name.

Revision ID: 015
"""

from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("git_token", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "git_token")
