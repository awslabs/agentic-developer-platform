"""Deduplicate tenant request settlement without inferring historical debits."""

import sqlalchemy as sa

from alembic import op

revision = "074_budget_settlement_receipts"
down_revision = "073_kimi_k3_pricing"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "budget_settlement_receipts",
        sa.Column("allocation_key", sa.String(64), nullable=False),
        sa.Column("org_id", sa.String(255), primary_key=True),
        sa.Column("request_id", sa.String(255), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False),
    )


def downgrade():
    # Dropping debit receipts permits duplicate historical debits. Preserve them.
    raise RuntimeError("Settlement receipts must survive rollback; stop consumers and reconcile before removal")
