"""Replace uq_knowledge_assets_source_scope with partial index excluding removed.

Issue #3524: soft-deleted assets (status='removed') permanently block
re-registration because the existing unique index counts removed rows.
The app-level dup-check in register_asset already filters status != 'removed',
but the DB constraint does not — creating a mismatch that causes
UniqueViolationError on re-registration after soft-delete.

Fix: recreate the unique index as a partial index with
WHERE status != 'removed', aligning DB enforcement with app semantics.

Downgrade caveat: if removed-duplicate rows exist (same source_ref/scope with
one removed and one active), downgrade will fail because the non-partial index
cannot tolerate those rows. Hard-delete the removed duplicates before
downgrading.

Revision ID: 012_partial_unique_excl_removed
Revises: 011_widen_vuln_text_cols
Create Date: 2026-07-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "012_partial_unique_excl_removed"
down_revision: str = "011_widen_vuln_text_cols"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace unique index with partial index excluding removed assets."""
    # Drop the old (non-partial) unique index
    op.execute("DROP INDEX IF EXISTS uq_knowledge_assets_source_scope")

    # Recreate as partial: only enforce uniqueness for non-removed rows
    op.execute("""
        CREATE UNIQUE INDEX uq_knowledge_assets_source_scope
        ON knowledge_assets (
            source_ref,
            COALESCE(tenant_id, ''),
            COALESCE(owner_sub, '')
        )
        WHERE status != 'removed'
    """)


def downgrade() -> None:
    """Revert to non-partial unique index.

    WARNING: This will fail if removed-duplicate rows exist (same source_ref +
    scope with one removed and one active row). Hard-delete the removed
    duplicates before downgrading.
    """
    op.execute("DROP INDEX IF EXISTS uq_knowledge_assets_source_scope")

    op.execute("""
        CREATE UNIQUE INDEX uq_knowledge_assets_source_scope
        ON knowledge_assets (
            source_ref,
            COALESCE(tenant_id, ''),
            COALESCE(owner_sub, '')
        )
    """)
