"""Gateway-held report spool for protected worker identities (#7392).

Protected workers are denied direct S3 by their permissions boundary, so the
untrusted pending-report spool they used to keep in the logs bucket now lives
beside the run-report assignment itself. The document stays untrusted: the
worker validates it against its own assignment and only the locked ``/started``
receipt decides ownership.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "087_run_report_spool_document"
down_revision = "086_orchestration_node_metadata"
branch_labels = None
depends_on = None


def upgrade():
    doc = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column("orchestration_run_reports", sa.Column("spool_document", doc))


def downgrade():
    op.drop_column("orchestration_run_reports", "spool_document")
