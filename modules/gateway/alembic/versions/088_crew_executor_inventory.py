"""Durable Crew executor inventory, before checkpoint pointer migrations."""

import sqlalchemy as sa

from alembic import op

revision = "088_crew_executor_inventory"
down_revision = "087_run_report_spool_document"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "crew_executors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("team_id", sa.String(255)),
        sa.Column("target_kind", sa.String(32), nullable=False, server_default="eks-pod"),
        sa.Column("state", sa.String(32), nullable=False, server_default="provisioning"),
        sa.Column("target_ref", sa.String(512)),
        sa.Column("dev_box_id", sa.String(255)),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("lease_generation", sa.Integer()),
        sa.Column("lease_expires_at", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_stopped_at", sa.DateTime(timezone=True)),
        sa.Column("idle_policy", sa.String(255)),
        sa.Column("approved_by", sa.String(255)),
        sa.CheckConstraint("length(tenant_id) > 0", name="ck_crew_executors_tenant_id"),
        sa.CheckConstraint("length(user_id) > 0", name="ck_crew_executors_user_id"),
        sa.CheckConstraint("target_kind IN ('eks-pod', 'ec2-instance')", name="ck_crew_executors_target_kind"),
        sa.CheckConstraint("state IN ('provisioning', 'ready', 'stopped', 'ending', 'deleted')", name="ck_crew_executors_state"),
        sa.CheckConstraint("generation > 0", name="ck_crew_executors_generation"),
    )
    op.create_index(
        "uq_crew_executors_active_owner",
        "crew_executors",
        ["tenant_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'deleted'"),
        sqlite_where=sa.text("state <> 'deleted'"),
    )


def downgrade():
    op.drop_index("uq_crew_executors_active_owner", table_name="crew_executors")
    op.drop_table("crew_executors")
