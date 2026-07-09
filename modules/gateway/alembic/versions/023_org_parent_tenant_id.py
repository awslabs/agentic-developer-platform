"""Add parent_tenant_id to organizations for multi-org-to-tenant linking.

Issue #2954: Rule 3 — link multiple GitHub orgs to one tenant (many:many,
attach-forward-only). A linked org's row points at its parent tenant via
parent_tenant_id. The matcher resolves parent_tenant_id or id.

Revision ID: 023_org_parent_tenant_id
Revises: 022_installed_by_user_id
Create Date: 2026-07-09
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: I001

from alembic import op

revision: str = "023_org_parent_tenant_id"
down_revision: str = "022_installed_by_user_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add parent_tenant_id column to organizations."""
    op.add_column(
        "organizations",
        sa.Column("parent_tenant_id", sa.String(255), nullable=True),
    )
    # Self-FK: linked org points at the parent tenant's Organization row.
    op.create_foreign_key(
        "fk_organizations_parent_tenant_id",
        "organizations",
        "organizations",
        ["parent_tenant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_organizations_parent_tenant_id",
        "organizations",
        ["parent_tenant_id"],
    )


def downgrade() -> None:
    """Remove parent_tenant_id column."""
    op.drop_index("ix_organizations_parent_tenant_id", table_name="organizations")
    op.drop_constraint(
        "fk_organizations_parent_tenant_id",
        "organizations",
        type_="foreignkey",
    )
    op.drop_column("organizations", "parent_tenant_id")
