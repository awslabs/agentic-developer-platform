"""Widen budget_usage.total_tokens and request_count to BIGINT.

budget_usage rows are unbounded accumulators (ON CONFLICT ... DO UPDATE
adds every request's tokens into the same monthly row). The INTEGER
column overflowed at 2,147,483,647 tokens (~$1,600 of monthly usage),
after which every upsert failed with NumericValueOutOfRange. Because the
budget-usage-tracker Lambda batches records on one connection, the
aborted transaction also rolled back the usage_logs cost bridge, zeroing
per-run costs in Agent Activity.

Revision ID: 024_budget_usage_bigint_tokens
Revises: 023_org_parent_tenant_id
Create Date: 2026-07-10
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: I001

from alembic import op

revision: str = "024_budget_usage_bigint_tokens"
down_revision: str | None = "023_org_parent_tenant_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen accumulator columns from INTEGER to BIGINT."""
    op.alter_column(
        "budget_usage",
        "total_tokens",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )
    op.alter_column(
        "budget_usage",
        "request_count",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Narrow back to INTEGER (fails if any value exceeds int32 range)."""
    op.alter_column(
        "budget_usage",
        "request_count",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "budget_usage",
        "total_tokens",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
