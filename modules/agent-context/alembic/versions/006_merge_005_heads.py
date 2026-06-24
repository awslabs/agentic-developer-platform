"""Merge 005 branch heads (backfill + project_scoping).

Both 005_backfill_tenant_scope and 005_project_scoping independently depend on
004_add_tenant_isolation_columns. This empty merge migration unifies them into
a single linear chain so `alembic upgrade head` works without --heads.

Revision ID: 006_merge_005_heads
Revises: 005_backfill_tenant_scope, 005_project_scoping
Create Date: 2026-06-24
"""

from collections.abc import Sequence

from alembic import op  # noqa: F401

revision: str = "006_merge_005_heads"
down_revision: tuple[str, ...] = (
    "005_backfill_tenant_scope",
    "005_project_scoping",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Merge point — no schema changes."""
    pass


def downgrade() -> None:
    """Merge point — no schema changes."""
    pass
