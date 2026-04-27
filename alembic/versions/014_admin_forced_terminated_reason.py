"""Add admin_forced to ck_instances_terminated_reason.

Spec 003 (admin instance management) introduces a Force Terminate
admin action distinct from idle-reaper, user-request, project-deleted,
chat-deleted, and failure-driven terminations. The DB CHECK constraint
``ck_instances_terminated_reason`` and the Python ``TerminatedReason``
enum get the new value ``admin_forced`` so analytics / audit queries
can distinguish admin-initiated kills from every other path.

Forward-only down: refuses to drop the value if any rows already use
it (Principle VI — idempotent / forward-completion lifecycle).

Revision ID: 014
Revises: 013
Create Date: 2026-04-27
"""
from alembic import op
import sqlalchemy as sa


revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


_OLD_VALUES = "('idle_24h','user_request','failed','project_deleted','chat_deleted')"
_NEW_VALUES = "('idle_24h','user_request','failed','project_deleted','chat_deleted','admin_forced')"


def upgrade():
    op.drop_constraint("ck_instances_terminated_reason", "instances", type_="check")
    op.create_check_constraint(
        "ck_instances_terminated_reason",
        "instances",
        f"terminated_reason IN {_NEW_VALUES} OR terminated_reason IS NULL",
    )


def downgrade():
    # Refuse to roll back if any rows already use the new value — losing
    # them would silently rewrite the audit trail for admin actions.
    bind = op.get_bind()
    count = bind.execute(
        sa.text("SELECT COUNT(*) FROM instances WHERE terminated_reason = 'admin_forced'")
    ).scalar()
    if count and count > 0:
        raise RuntimeError(
            f"Cannot downgrade migration 014: {count} instances row(s) "
            "use terminated_reason='admin_forced'. Re-classify them first."
        )
    op.drop_constraint("ck_instances_terminated_reason", "instances", type_="check")
    op.create_check_constraint(
        "ck_instances_terminated_reason",
        "instances",
        f"terminated_reason IN {_OLD_VALUES} OR terminated_reason IS NULL",
    )
