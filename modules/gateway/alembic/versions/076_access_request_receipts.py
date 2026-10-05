"""Durable exact access-request decision receipts."""

from alembic import op
import sqlalchemy as sa

revision = "076_access_request_receipts"
down_revision = "075_membership_revocation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tenant_access_requests", sa.Column("decision_receipt", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("tenant_access_requests", "decision_receipt")
