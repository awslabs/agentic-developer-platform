"""Retain the original admin reconciliation response for idempotent replay."""

import sqlalchemy as sa

from alembic import op

revision = "116_metering_admin_result"
down_revision = "115_legacy_metering_cohort"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("metering_reconciliation_events", sa.Column("result_snapshot", sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError("Metering reconciliation evidence must survive rollback")
