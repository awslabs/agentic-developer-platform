"""Persist scoped publication retry receipts before scheduling another attempt.

Revision ID: l5_publication_retry_receipts
Revises: l5_publication_prevention_merge
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l5_publication_retry_receipts"
down_revision = "l5_publication_prevention_merge"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "l5_service_publication_retry_receipts",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("product_id", sa.String(255), primary_key=True),
        sa.Column("project_id", sa.String(255), primary_key=True),
        sa.Column("service_id", sa.String(255), primary_key=True),
        sa.Column("actor_id", sa.String(255), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("publication_id", sa.String(36), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response", payload, nullable=False),
    )


def downgrade():
    op.drop_table("l5_service_publication_retry_receipts")
