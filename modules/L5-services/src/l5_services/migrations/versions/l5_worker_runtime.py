"""Retain native job identity between bounded invocations.

Revision ID: l5_worker_runtime
Revises: l5_worker_evidence
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l5_worker_runtime"
down_revision = "l5_worker_evidence"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.add_column("l5_service_runs", sa.Column("native_job_ref", payload))


def downgrade():
    op.drop_column("l5_service_runs", "native_job_ref")
