"""Accept projects.mode='container' and enforce the closed enum.

Today `projects.mode` is a free-form String(10) (existing values: 'host',
'docker'). This migration introduces a CHECK constraint that closes the
enum to {'host','docker','container'} AND flips the default for newly
inserted rows to 'container' (FR-035).

Existing rows keep their current value — FR-034.

Revision ID: 012
Revises: 011
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade():
    # Flip server-side default to 'container' for new inserts. Existing rows
    # are untouched — they keep whatever value they have (FR-034).
    op.alter_column(
        "projects",
        "mode",
        server_default=sa.text("'container'"),
        existing_type=sa.String(10),
        existing_nullable=False,
    )
    # Close the enum at the DB layer.
    op.create_check_constraint(
        "ck_projects_mode",
        "projects",
        "mode IN ('host','docker','container')",
    )


def downgrade():
    op.drop_constraint("ck_projects_mode", "projects", type_="check")
    op.alter_column(
        "projects",
        "mode",
        server_default=sa.text("'docker'"),
        existing_type=sa.String(10),
        existing_nullable=False,
    )
