"""Add member_approval_policy to organizations.

Issue #719: Multi-user tenant matching — per-tenant policy controls whether
new members from the same GitHub org are auto-approved or require admin approval.

Revision ID: 014_member_approval_policy
Revises: 013_channel_tenant_map_metadata
Create Date: 2026-05-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "014_member_approval_policy"
down_revision: str | None = "013_channel_tenant_map_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add member_approval_policy column with default 'auto_approve_org_members'."""
    op.add_column(
        "organizations",
        sa.Column(
            "member_approval_policy",
            sa.String(32),
            nullable=False,
            server_default="auto_approve_org_members",
        ),
    )


def downgrade() -> None:
    """Remove member_approval_policy column."""
    op.drop_column("organizations", "member_approval_policy")
