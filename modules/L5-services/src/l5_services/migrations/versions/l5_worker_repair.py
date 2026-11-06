"""Track exhausted transport redrive separately from execution.

Revision ID: l5_worker_repair
Revises: l5_worker_attempts
"""

from alembic import op
import sqlalchemy as sa

revision = "l5_worker_repair"
down_revision = "l5_worker_attempts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("l5_service_work", sa.Column("blocked_at", sa.DateTime(timezone=True)))
    op.create_index("ix_l5_work_repair", "l5_service_work", ["completed_at", "blocked_at", "due_at", "lease_until"])


def downgrade():
    op.drop_index("ix_l5_work_repair", table_name="l5_service_work")
    op.drop_column("l5_service_work", "blocked_at")
