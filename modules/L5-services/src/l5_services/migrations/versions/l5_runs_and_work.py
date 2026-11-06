"""Add scoped execution runs, idempotency receipts and durable work.

Revision ID: l5_runs_and_work
Revises: l5_configurations
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l5_runs_and_work"
down_revision = "l5_configurations"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "l5_service_runs",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("service_id", sa.String(255), nullable=False),
        sa.Column("configuration_id", sa.String(36), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), nullable=False),
        sa.Column("binding_id", sa.String(255), nullable=False),
        sa.Column("trigger_id", sa.String(255), nullable=False),
        sa.Column("binding_generation", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("input", payload, nullable=False),
        sa.Column("admission", payload, nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("cancel_reason", sa.String(1000)),
        sa.Column("provider_state", sa.String(32), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "configuration_id", "configuration_revision"],
            ["l5_service_config_versions.tenant_id", "l5_service_config_versions.configuration_id",
             "l5_service_config_versions.revision"],
        ),
    )
    op.create_table(
        "l5_service_receipts",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("product_id", sa.String(255), primary_key=True),
        sa.Column("project_id", sa.String(255), primary_key=True),
        sa.Column("service_id", sa.String(255), primary_key=True),
        sa.Column("actor_id", sa.String(255), primary_key=True),
        sa.Column("operation", sa.String(32), primary_key=True),
        sa.Column("idempotency_key", sa.String(255), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
    )
    op.create_table(
        "l5_service_work",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("work_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatch_owner", sa.String(255)),
        sa.Column("dispatch_until", sa.DateTime(timezone=True)),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True)),
        sa.Column("redeliver_after", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(255)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("fence", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(1024)),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
        sa.UniqueConstraint("tenant_id", "run_id", "kind", "generation", "step"),
    )
    op.create_index("ix_l5_work_dispatch", "l5_service_work", ["completed_at", "due_at", "redeliver_after"])


def downgrade():
    op.drop_index("ix_l5_work_dispatch", table_name="l5_service_work")
    op.drop_table("l5_service_work")
    op.drop_table("l5_service_receipts")
    op.drop_table("l5_service_runs")
