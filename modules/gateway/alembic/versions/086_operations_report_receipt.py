"""Persist bounded operations reports on their authenticated run assignments."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "086_operations_report_receipt"
down_revision = "085_amendment_issue_bindings"
branch_labels = None
depends_on = None


def upgrade():
    doc = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column("orchestration_run_reports", sa.Column("operations_report_receipt", doc))


def downgrade():
    op.drop_column("orchestration_run_reports", "operations_report_receipt")
