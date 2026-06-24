"""Add tenant_id and owner_sub columns to repositories for multi-tenant isolation.

Issue #1770: Foundational migration for E8 (#1721) — tenant & individual isolation.
Adds nullable tenant_id and owner_sub columns so every indexed repo can be scope-stamped.
Existing rows remain tenant_id=NULL (shared corpus). No behaviour change — purely additive.

Scoping semantics:
  - tenant_id IS NULL → shared (all authorized callers, existing behavior)
  - tenant_id IS NOT NULL, owner_sub IS NULL → per-tenant (org-scoped)
  - owner_sub IS NOT NULL → per-individual (user-scoped)

Revision ID: 004_add_tenant_isolation_columns
Revises: 003_index_run_stages
Create Date: 2026-06-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "004_add_tenant_isolation_columns"
down_revision: str = "003_index_run_stages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add tenant_id and owner_sub columns with indexes to repositories."""
    # -------------------------------------------------------------------------
    # Add tenant isolation columns (nullable — existing rows stay NULL = shared)
    # -------------------------------------------------------------------------
    op.execute("""
        ALTER TABLE repositories
        ADD COLUMN tenant_id VARCHAR(256)
    """)

    op.execute("""
        ALTER TABLE repositories
        ADD COLUMN owner_sub VARCHAR(256)
    """)

    # -------------------------------------------------------------------------
    # Indexes for efficient scoped queries
    # -------------------------------------------------------------------------
    op.create_index("ix_repositories_tenant_id", "repositories", ["tenant_id"])
    op.create_index("ix_repositories_owner_sub", "repositories", ["owner_sub"])


def downgrade() -> None:
    """Remove tenant isolation columns and their indexes."""
    op.drop_index("ix_repositories_owner_sub", table_name="repositories")
    op.drop_index("ix_repositories_tenant_id", table_name="repositories")

    op.execute("ALTER TABLE repositories DROP COLUMN owner_sub")
    op.execute("ALTER TABLE repositories DROP COLUMN tenant_id")
