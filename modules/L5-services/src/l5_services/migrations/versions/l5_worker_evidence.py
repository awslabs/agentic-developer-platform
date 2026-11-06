"""Store sealed source and immutable native evidence.

Revision ID: l5_worker_evidence
Revises: l5_admission_recovery
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l5_worker_evidence"
down_revision = "l5_admission_recovery"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.add_column("l5_service_runs", sa.Column("capture_limits", payload))
    op.add_column("l5_service_runs", sa.Column("manifest", payload))
    op.add_column("l5_service_runs", sa.Column("monitoring", payload))
    op.create_table(
        "l5_service_provider_observations",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("observation_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("work_id", sa.String(36), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", payload, nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
        sa.UniqueConstraint("tenant_id", "run_id", "work_id"),
    )
    op.create_table(
        "l5_service_results",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("result_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", payload, nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
        sa.UniqueConstraint("tenant_id", "run_id", "version"),
    )


def downgrade():
    op.drop_table("l5_service_results")
    op.drop_table("l5_service_provider_observations")
    op.drop_column("l5_service_runs", "monitoring")
    op.drop_column("l5_service_runs", "manifest")
    op.drop_column("l5_service_runs", "capture_limits")
