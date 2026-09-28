"""Preserve an audit trail for incident pricing credits."""

import sqlalchemy as sa

from alembic import op

revision = "079_budget_pricing_corrections"
down_revision = "078_gpt6_sol_luna_pricing"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "budget_pricing_corrections",
        sa.Column("org_id", sa.String(255), primary_key=True),
        sa.Column("request_id", sa.String(255), primary_key=True),
        sa.Column("correction_id", sa.String(64), primary_key=True),
        sa.Column("credit_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("original_decision", sa.JSON(), nullable=False),
        sa.Column("corrected_decision", sa.JSON(), nullable=False),
        sa.Column("allocation_key", sa.String(64), nullable=False),
        sa.Column("source_key", sa.String(1024), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("Pricing credits and their audit trail must survive application rollback")
