"""Enforce one installation -> one tenant via a partial unique index.

Issue #4070 (sub-EPIC #4068 ·A0), scope item 3: the uniqueness guard.

Why this is a SEPARATE migration from 026
-----------------------------------------
Two mechanical reasons, not stylistic ones:

1. ``CREATE UNIQUE INDEX CONCURRENTLY`` cannot run inside a transaction, so it
   needs ``autocommit_block()`` and cannot share one with 026's dedup DML.
2. The dedup MUST have committed before the constraint is applied. Adding the
   constraint against still-duplicated rows fails the migration and blocks the
   deploy — the ordering is the whole point of splitting the pair.

CONCURRENTLY keeps this online: no writer lock on ``channel_tenant_map``.

Postgres-only. SQLite supports neither CONCURRENTLY nor partial indexes, so on
SQLite the invariant is enforced by the resolver (which fails closed on
AMBIGUOUS) and asserted at the application level in tests.

Note on quarantined rows: rows flagged ``ownership_disputed`` by 026 are
EXCLUDED from the index. They are known duplicates that decision D3 deliberately
declined to resolve, so including them would make this migration fail on exactly
the deployments that need it most. The resolver denies access to them
independently, so excluding them from the constraint does not open a hole — it
keeps a blocked deploy from being the only way to learn a conflict exists.

The exclusion is driven by a boolean COLUMN, not by a subquery against
``installation_ownership_conflicts``: Postgres partial-index predicates may only
reference columns of the indexed table, so a ``NOT EXISTS (...)`` predicate is
rejected outright. 026 denormalizes the conflict flag onto the row for exactly
this reason.

Revision ID: 027_install_tenant_unique
Revises: 026_ctm_installation_id
Create Date: 2026-08-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "027_install_tenant_unique"
down_revision: str | None = "026_ctm_installation_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_channel_tenant_map_installation_id"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: no CONCURRENTLY, no partial indexes. Enforced by the resolver.
        return

    with op.get_context().autocommit_block():
        op.execute(
            f"""
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}
                ON channel_tenant_map (installation_id)
             WHERE installation_id IS NOT NULL
               AND ownership_disputed = false
            """
        )


def downgrade() -> None:
    """Drop the unique index. Reversible and data-preserving."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
