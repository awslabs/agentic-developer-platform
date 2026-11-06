"""Add native DevOps execution identity without synthesizing admission.

Revision ID: l5_devops_native_runs
Revises: l5_results_knowledge_merge
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l5_devops_native_runs"
down_revision = "l5_results_knowledge_merge"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _admission_nullable(nullable):
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("l5_service_runs", recreate="always") as batch:
            batch.alter_column("admission", existing_type=payload, nullable=nullable)
    else:
        op.alter_column("l5_service_runs", "admission", existing_type=payload, nullable=nullable)


def upgrade():
    op.add_column("l5_service_bindings", sa.Column("next_check_at", sa.DateTime(timezone=True)))
    op.create_index("ix_l5_devops_binding_checks", "l5_service_bindings", ["service_id", "state", "next_check_at"])
    _admission_nullable(True)
    op.add_column("l5_service_runs", sa.Column("origin", sa.String(16), nullable=False, server_default="adp_submitted"))
    op.add_column("l5_service_runs", sa.Column("scope", payload))
    op.add_column("l5_service_runs", sa.Column("next_check_at", sa.DateTime(timezone=True)))
    op.add_column("l5_service_runs", sa.Column("provider_readback_ref", payload))
    op.create_index("ix_l5_native_run_checks", "l5_service_runs", ["service_id", "next_check_at"])
    op.create_table(
        "l5_devops_native_run_identities",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("connection_id", sa.String(255), primary_key=True),
        sa.Column("space_id", sa.String(255), primary_key=True),
        sa.Column("execution_id", sa.String(255), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False, unique=True),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
    )
    op.create_table(
        "l5_devops_native_event_receipts",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("connection_id", sa.String(255), primary_key=True),
        sa.Column("space_id", sa.String(255), primary_key=True),
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
    )


def downgrade():
    op.drop_table("l5_devops_native_event_receipts")
    op.drop_table("l5_devops_native_run_identities")
    op.drop_index("ix_l5_native_run_checks", table_name="l5_service_runs")
    op.drop_column("l5_service_runs", "provider_readback_ref")
    op.drop_column("l5_service_runs", "next_check_at")
    op.drop_column("l5_service_runs", "scope")
    op.drop_column("l5_service_runs", "origin")
    _admission_nullable(False)
    op.drop_index("ix_l5_devops_binding_checks", table_name="l5_service_bindings")
    op.drop_column("l5_service_bindings", "next_check_at")
