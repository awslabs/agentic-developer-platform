"""Backfill tenant_id for existing private repos.

Issue #1771: One-time data migration to scope-stamp existing private repositories.
Private repos (allowed_principals != '["*"]') get tenant_id = owner.
Public repos (allowed_principals = '["*"]') remain tenant_id = NULL (shared corpus).

This is idempotent: rows that already have a tenant_id are not touched.

Scoping rule (from E8 design doc, section 6):
  - allowed_principals = ["*"] → public → tenant_id stays NULL (shared)
  - allowed_principals != ["*"] → private → tenant_id = owner (org-scoped)

Revision ID: 005_backfill_tenant_scope
Revises: 004_add_tenant_isolation_columns
Create Date: 2026-06-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "005_backfill_tenant_scope"
down_revision: str = "004_add_tenant_isolation_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill tenant_id = owner for private repos (allowed_principals != '["*"]').

    Only touches rows where tenant_id IS NULL to remain idempotent.
    Public repos (allowed_principals = '["*"]') are explicitly excluded.
    """
    op.execute("""
        UPDATE repositories
        SET tenant_id = owner,
            updated_at = NOW()
        WHERE tenant_id IS NULL
          AND allowed_principals != '["*"]'::jsonb
    """)


def downgrade() -> None:
    """Re-null tenant_id for rows that were backfilled.

    Only clears tenant_id for rows matching the backfill criteria (private repos
    whose tenant_id equals their owner). Does not touch manually-set tenant_ids
    that don't match this pattern.
    """
    op.execute("""
        UPDATE repositories
        SET tenant_id = NULL,
            updated_at = NOW()
        WHERE tenant_id = owner
          AND allowed_principals != '["*"]'::jsonb
    """)
