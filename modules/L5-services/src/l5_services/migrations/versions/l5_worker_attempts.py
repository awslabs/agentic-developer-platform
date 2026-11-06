"""Persist assessment effect identity before any provider start.

Revision ID: l5_worker_attempts
Revises: l5_worker_runtime
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l5_worker_attempts"
down_revision = "l5_worker_runtime"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "l5_service_worker_attempts",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("first_work_id", sa.String(36), nullable=False),
        sa.Column("first_fence", sa.Integer(), nullable=False),
        sa.Column("idempotency_token", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("native_job_ref", payload),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
        sa.UniqueConstraint("tenant_id", "run_id", "operation"),
    )


def downgrade():
    op.drop_table("l5_service_worker_attempts")
