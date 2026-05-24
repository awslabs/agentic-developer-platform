"""Add user_kind and bot_kind columns to users table.

Issue #780: Bot identity rows — adds user_kind discriminator ('human'|'bot'),
bot_kind slug, CHECK constraint, and index for efficient bot queries.

Revision ID: 015_bot_users
Revises: 014_member_approval_policy
Create Date: 2026-05-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "015_bot_users"
down_revision: str | None = "014_member_approval_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add user_kind and bot_kind columns with CHECK constraint and index."""
    op.add_column(
        "users",
        sa.Column(
            "user_kind",
            sa.String(16),
            nullable=False,
            server_default="human",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "bot_kind",
            sa.String(64),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "users_user_kind_chk",
        "users",
        "user_kind IN ('human', 'bot')",
    )
    op.create_index("ix_users_user_kind", "users", ["user_kind"])


def downgrade() -> None:
    """Remove user_kind, bot_kind columns, constraint, and index."""
    op.drop_index("ix_users_user_kind", table_name="users")
    op.drop_constraint("users_user_kind_chk", "users", type_="check")
    op.drop_column("users", "bot_kind")
    op.drop_column("users", "user_kind")
