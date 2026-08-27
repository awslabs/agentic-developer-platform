"""Widen budget_usage.total_cost_usd to NUMERIC(14,6).

Issue #4287 (Wave 3 of #4075).

``budget_usage.total_cost_usd`` was NUMERIC(10,2) — two decimal places — but
cost is computed to six (``pricing.calculate_cost`` returns ``round(x, 6)``) and
``usage_logs.cost_usd`` already stores NUMERIC(10,6). The budget ledger was
therefore the only place in the pipeline that rounded to cents.

For sub-cent models that rounds to nothing. A haiku request at $0.00025/1k input
costs well under a cent, so ``usage.total_cost_usd += cost`` in
``_record_entity_usage`` quantized it to 0.00 — a burst of small requests
accumulated unbounded real spend while the denominator the cap is enforced
against stayed at zero. That is the same soundness hole #4287 exists to close,
arriving through the schema instead of through concurrency.

Widening precision and scale is forward-safe: every existing value is
representable, and NUMERIC is exact so no stored figure changes.

The integral range is deliberately widened too (10,2 → 14,6 keeps 8 integral
digits, so the maximum representable total goes UP, not down). Going to (10,6)
instead would have left only 4 integral digits and made any row above $9,999.99
unstorable — an outage on the ledger write path for exactly the biggest spenders.

**The downgrade is LOSSY.** Narrowing back to (10,2) rounds every accumulated
total to cents, permanently discarding the sub-cent precision this migration
exists to keep. Reverting the #4287 PR therefore does NOT restore the prior state
byte-for-byte: the code reverts cleanly, the data does not. Prefer leaving the
column wide (harmless to the old code, which simply writes 2dp values into it)
over running the downgrade.

Revision ID: 030_budget_usage_cost_precision
Revises: 029_orchestration_graph
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: I001

from alembic import op

revision: str = "030_budget_usage_cost_precision"
down_revision: str | None = "029_orchestration_graph"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen the cost accumulator so sub-cent spend stops rounding to zero."""
    op.alter_column(
        "budget_usage",
        "total_cost_usd",
        existing_type=sa.Numeric(10, 2),
        type_=sa.Numeric(14, 6),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Narrow back to NUMERIC(10,2). LOSSY — see the module docstring.

    Postgres rounds on the ALTER rather than erroring, so this succeeds and
    silently drops sub-cent precision from every row.
    """
    op.alter_column(
        "budget_usage",
        "total_cost_usd",
        existing_type=sa.Numeric(14, 6),
        type_=sa.Numeric(10, 2),
        existing_nullable=False,
    )
