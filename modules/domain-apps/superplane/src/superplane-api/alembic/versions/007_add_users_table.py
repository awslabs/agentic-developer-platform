"""Add users table for RBAC user management.

Revision ID: 007_add_users_table
Revises: 007_add_org_settings_sso
Create Date: 2026-04-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "007_add_users_table"
down_revision = "007_add_org_settings_sso"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("cognito_sub", sa.String(255), nullable=True),
        sa.Column("role", sa.String(50), nullable=False, server_default="developer"),
        sa.Column("status", sa.String(50), nullable=False, server_default="invited"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_users_cognito_sub", "users", ["cognito_sub"], unique=True)
    op.create_index("ix_users_org_id", "users", ["org_id"])
    op.create_index("ix_users_org_email", "users", ["org_id", "email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_org_email", table_name="users")
    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_index("ix_users_cognito_sub", table_name="users")
    op.drop_table("users")
