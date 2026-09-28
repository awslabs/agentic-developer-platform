"""Index budget_usage by (entity_type, entity_id) — Issue #4627 review fix.

The mis-partitioned-cap report's cross-partition existence check
(``report_routes._keys_accruing_elsewhere``) predicates on
``entity_type + entity_id IN (...) + org_id != :org`` — and every existing index
on ``budget_usage`` leads with ``org_id``, which a negated predicate cannot use.
Without this index each report call full-scans a ledger that grows without
pruning (the tracker upserts daily/weekly/monthly rows per entity forever),
holding an async pool connection on the same table the per-request enforcement
hot path reads and writes.

Revision id kept ≤32 chars (VARCHAR(32) — the #4123 class; SQLite hides it).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "035_budget_usage_entity_key"
down_revision: str | Sequence[str] | None = "034_person_budget_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_budget_usage_entity_key",
        "budget_usage",
        ["entity_type", "entity_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_budget_usage_entity_key", table_name="budget_usage")
