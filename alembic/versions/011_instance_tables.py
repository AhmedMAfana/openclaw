"""Per-chat isolated instances.

Adds two new tables:
  - `instances`        — one row per active dev environment bound to a chat
  - `instance_tunnels` — public networking surface (CF named tunnel) per instance

And two nullable FK extensions:
  - `web_chat_sessions.instance_id` (SET NULL on delete)
  - `tasks.instance_id`             (CASCADE on delete)

The design is specified in specs/001-per-chat-instances/data-model.md.
Partial unique indexes enforce Constitution Principle I (one active instance
per chat) and Principle VI (one active tunnel per instance) at the DB layer.

Revision ID: 011
Revises: 010
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade():
    # --- instances ----------------------------------------------------------
    op.create_table(
        "instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("slug", sa.String(20), nullable=False),
        sa.Column("chat_session_id", sa.Integer,
                  sa.ForeignKey("web_chat_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("project_id", sa.Integer,
                  sa.ForeignKey("projects.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default=sa.text("'provisioning'")),
        sa.Column("compose_project", sa.String(30), nullable=False),
        sa.Column("workspace_path", sa.String(255), nullable=False),
        sa.Column("session_branch", sa.String(255), nullable=False),
        sa.Column("image_digest", sa.String(255), nullable=True),
        sa.Column("resource_profile", sa.String(20), nullable=False,
                  server_default=sa.text("'standard'")),
        sa.Column("heartbeat_secret", sa.String(64), nullable=False),
        sa.Column("db_password", sa.String(64), nullable=False),
        sa.Column("per_user_count_at_provision", sa.SmallInteger, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grace_notification_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminated_reason", sa.String(30), nullable=True),
        sa.Column("failure_code", sa.String(30), nullable=True),
        sa.Column("failure_message", sa.Text, nullable=True),
        sa.UniqueConstraint("slug", name="uq_instances_slug"),
        # Principle I: one active instance per chat. Partial unique index.
        sa.CheckConstraint(
            "status IN ('provisioning','running','idle','terminating','destroyed','failed')",
            name="ck_instances_status",
        ),
        # Principle III/V safety: slug format is strict — keeps SQL-level strings
        # safe to interpolate into DNS names, compose project names, volume names.
        sa.CheckConstraint(
            r"slug ~ '^inst-[0-9a-f]{14}$'",
            name="ck_instances_slug_format",
        ),
        sa.CheckConstraint(
            "terminated_reason IN "
            "('idle_24h','user_request','failed','project_deleted','chat_deleted') "
            "OR terminated_reason IS NULL",
            name="ck_instances_terminated_reason",
        ),
        sa.CheckConstraint(
            "failure_code IN "
            "('image_build','compose_up','projctl_up','tunnel_provision','dns',"
            "'health_check','oom','storage_full','orchestrator_crash','unknown') "
            "OR failure_code IS NULL",
            name="ck_instances_failure_code",
        ),
    )
    op.create_index(
        "idx_instances_status_expires",
        "instances",
        ["status", "expires_at"],
    )
    op.create_index("idx_instances_chat", "instances", ["chat_session_id"])
    op.create_index("idx_instances_project", "instances", ["project_id"])
    # Partial unique index — one active instance per chat.
    op.execute(
        "CREATE UNIQUE INDEX uq_instances_active_per_chat "
        "ON instances (chat_session_id) "
        "WHERE status IN ('provisioning','running','idle','terminating')"
    )

    # --- instance_tunnels ---------------------------------------------------
    op.create_table(
        "instance_tunnels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("instances.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("cf_tunnel_id", sa.String(64), nullable=False),
        sa.Column("cf_tunnel_name", sa.String(40), nullable=False),
        sa.Column("web_hostname", sa.String(255), nullable=False),
        sa.Column("hmr_hostname", sa.String(255), nullable=False),
        sa.Column("ide_hostname", sa.String(255), nullable=True),
        # Reference (Docker secret name), NOT the credential JSON — Principle IV.
        sa.Column("credentials_secret", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default=sa.text("'provisioning'")),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("cf_tunnel_name", name="uq_instance_tunnels_cf_name"),
        sa.CheckConstraint(
            "status IN ('provisioning','active','rotating','destroyed')",
            name="ck_instance_tunnels_status",
        ),
    )
    op.create_index(
        "idx_instance_tunnels_instance", "instance_tunnels", ["instance_id"]
    )
    # Principle VI: exactly one active tunnel per instance.
    op.execute(
        "CREATE UNIQUE INDEX uq_instance_tunnels_one_active "
        "ON instance_tunnels (instance_id) WHERE status = 'active'"
    )

    # --- web_chat_sessions.instance_id FK ----------------------------------
    op.add_column(
        "web_chat_sessions",
        sa.Column(
            "instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instances.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # --- tasks.instance_id FK ----------------------------------------------
    op.add_column(
        "tasks",
        sa.Column(
            "instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instances.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("tasks", "instance_id")
    op.drop_column("web_chat_sessions", "instance_id")
    op.execute("DROP INDEX IF EXISTS uq_instance_tunnels_one_active")
    op.drop_index("idx_instance_tunnels_instance", table_name="instance_tunnels")
    op.drop_table("instance_tunnels")
    op.execute("DROP INDEX IF EXISTS uq_instances_active_per_chat")
    op.drop_index("idx_instances_project", table_name="instances")
    op.drop_index("idx_instances_chat", table_name="instances")
    op.drop_index("idx_instances_status_expires", table_name="instances")
    op.drop_table("instances")
