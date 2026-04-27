"""Add audit_logs.instance_slug so Principle I enforcement is queryable.

Constitution Principle I requires every MCP tool call to be recorded
with `{instance_slug, chat_session_id, task_id}` so a post-hoc auditor
can assert one-instance-per-task. The `audit_logs.metadata` JSONB
column carried that data informally; this migration promotes
`instance_slug` to a first-class column so cross-chat isolation
queries (T032's premise, T092's soak) can use an index instead of a
JSONB probe.

Also closes a silent no-op in
`services/chat_session_service.delete_chat_cascade`, whose audit
cleanup was guarded by `hasattr(AuditLog, "instance_slug")` — true
after this migration, false before it, quietly dropping FR-013b's
slug-keyed audit sweep.

Backwards-compatible: column is nullable, existing rows stay NULL.

Revision ID: 013
Revises: 012
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa


revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "audit_logs",
        sa.Column(
            "instance_slug",
            sa.String(20),
            nullable=True,
        ),
    )
    # Scoped index: we filter WHERE instance_slug = <slug> for cascade
    # cleanup and adversarial cross-chat audits. A full-column index is
    # cheap because most rows will have a value going forward.
    op.create_index(
        "idx_audit_logs_instance_slug",
        "audit_logs",
        ["instance_slug"],
    )


def downgrade():
    op.drop_index("idx_audit_logs_instance_slug", table_name="audit_logs")
    op.drop_column("audit_logs", "instance_slug")
