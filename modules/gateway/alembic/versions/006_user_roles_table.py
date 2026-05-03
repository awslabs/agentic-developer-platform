"""Add user_roles table for identity Phase A.1.

Issue #387: Tracks role assignments per user per org with audit trail.

Revision ID: 006_user_roles
Revises: 005_identity_columns
Create Date: 2026-05-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "006_user_roles"
down_revision: str | None = "005_identity_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create user_roles table."""
    op.create_table(
        "user_roles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("org_id", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, comment="admin | member"),
        sa.Column("granted_by_user_id", sa.String(length=255), nullable=True),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("user_id", "org_id", "role", name="uq_user_roles_user_org_role"),
    )
    op.create_index("ix_user_roles_user_id", "user_roles", ["user_id"])
    op.create_index("ix_user_roles_org_id", "user_roles", ["org_id"])


def downgrade() -> None:
    """Drop user_roles table."""
    op.drop_index("ix_user_roles_org_id", table_name="user_roles")
    op.drop_index("ix_user_roles_user_id", table_name="user_roles")
    op.drop_table("user_roles")
