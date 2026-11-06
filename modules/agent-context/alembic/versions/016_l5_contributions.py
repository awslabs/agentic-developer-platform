"""Durable immutable L5 projections and task receipts owned by Context.

Revision ID: 016_l5_contributions
Revises: 015_product_knowledge
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "016_l5_contributions"
down_revision = "015_product_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("l5_contribution_records",
        sa.Column("record_key", sa.String(256), primary_key=True),
        sa.Column("document", postgresql.JSONB, nullable=False),
        sa.Column("revision", sa.BigInteger, nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_l5_contribution_revision"))


def downgrade():
    # Tombstones must survive code rollback, otherwise replay can resurrect data.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM l5_contribution_records) THEN
            RAISE EXCEPTION 'Cannot discard durable L5 history';
        END IF;
    END $$""")
    op.drop_table("l5_contribution_records")
