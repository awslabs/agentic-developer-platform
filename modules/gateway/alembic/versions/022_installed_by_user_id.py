"""Add installed_by_user_id to channel_tenant_map.

Issue #3073: Per-connection install ownership — the user who installs the
GitHub App can manage (disconnect) that connection without workspace admin role.
Nullable FK to users.id (ON DELETE SET NULL); pre-existing rows stay NULL
(managed by workspace admins via existing paths).

Revision ID: 022_installed_by_user_id
Revises: 021_tenant_memberships
Create Date: 2026-07-06
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "022_installed_by_user_id"
down_revision: str = "021_tenant_memberships"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add installed_by_user_id column to channel_tenant_map."""
    op.add_column(
        "channel_tenant_map",
        sa.Column(
            "installed_by_user_id",
            sa.String(length=255),
            nullable=True,
        ),
    )

    # FK constraint — only on PostgreSQL (SQLite has limited ALTER TABLE).
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_channel_tenant_map_installed_by_user_id",
            "channel_tenant_map",
            "users",
            ["installed_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Remove installed_by_user_id column."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "fk_channel_tenant_map_installed_by_user_id",
            "channel_tenant_map",
            type_="foreignkey",
        )
    op.drop_column("channel_tenant_map", "installed_by_user_id")
