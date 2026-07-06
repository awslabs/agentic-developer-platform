"""Add github_org_id and github_app_id to organizations.

Issue #2952: Register org → create org-tenant shell. The github_org_id column
stores the stable numeric GitHub organization ID for keying the org-tenant
mapping. github_app_id stores the GitHub App ID for registry seeding (D11).

Both are nullable (existing orgs pre-date this) and indexed for lookup from
install_callback's org-resolution path.

Revision ID: 020_org_github_ids
Revises: 019_knowledge_assets
Create Date: 2026-07-05
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: I001

from alembic import op

revision: str = "020_org_github_ids"
down_revision: str = "019_knowledge_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add github_org_id and github_app_id columns to organizations."""
    op.add_column(
        "organizations",
        sa.Column("github_org_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("github_app_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_organizations_github_org_id",
        "organizations",
        ["github_org_id"],
    )
    op.create_index(
        "ix_organizations_github_app_id",
        "organizations",
        ["github_app_id"],
    )


def downgrade() -> None:
    """Remove github_org_id and github_app_id columns."""
    op.drop_index("ix_organizations_github_app_id", table_name="organizations")
    op.drop_index("ix_organizations_github_org_id", table_name="organizations")
    op.drop_column("organizations", "github_app_id")
    op.drop_column("organizations", "github_org_id")
