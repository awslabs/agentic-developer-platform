"""Add Cognito fields to departments and users tables

Revision ID: 3e525b1eg90e
Revises: 2d414a0df89d
Create Date: 2026-02-14 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3e525b1eg90e"
down_revision: str | None = "2d414a0df89d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Cognito-related fields to support Cognito authentication.

    This migration adds:
    - departments: cognito_group_name, description, budget_limit, created_at, updated_at
    - teams: description, created_at, updated_at
    - users: name, role, cognito_sub, cognito_username, created_at, updated_at
    - service_accounts: description
    """
    # Add new columns to departments table
    with op.batch_alter_table("departments") as batch_op:
        batch_op.add_column(sa.Column("cognito_group_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("budget_limit", sa.Numeric(precision=15, scale=2), nullable=True))
        batch_op.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))

    # Add new columns to teams table
    with op.batch_alter_table("teams") as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))

    # Add new columns to users table
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("role", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("cognito_sub", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("cognito_username", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))

    # Create index on cognito_sub for efficient lookups
    op.create_index(op.f("ix_users_cognito_sub"), "users", ["cognito_sub"], unique=False)

    # Add description column to service_accounts table
    with op.batch_alter_table("service_accounts") as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove Cognito-related fields."""
    # Remove description from service_accounts
    with op.batch_alter_table("service_accounts") as batch_op:
        batch_op.drop_column("description")

    # Remove index on cognito_sub
    op.drop_index(op.f("ix_users_cognito_sub"), table_name="users")

    # Remove new columns from users table
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("created_at")
        batch_op.drop_column("cognito_username")
        batch_op.drop_column("cognito_sub")
        batch_op.drop_column("role")
        batch_op.drop_column("name")

    # Remove new columns from teams table
    with op.batch_alter_table("teams") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("created_at")
        batch_op.drop_column("description")

    # Remove new columns from departments table
    with op.batch_alter_table("departments") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("created_at")
        batch_op.drop_column("budget_limit")
        batch_op.drop_column("description")
        batch_op.drop_column("cognito_group_name")
