"""Retain Transform native absence receipts beside binding operations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l5_transform_cleanup"
down_revision = "l5_publication_prevention_merge"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.add_column("l5_service_binding_operations", sa.Column("cleanup_receipt", payload))


def downgrade():
    op.drop_column("l5_service_binding_operations", "cleanup_receipt")
