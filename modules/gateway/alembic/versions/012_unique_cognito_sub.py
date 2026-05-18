"""Add unique partial index on users.cognito_sub WHERE cognito_sub IS NOT NULL.

Issue #700: prevent duplicate canonical user rows for the same Cognito identity.

Uses CREATE UNIQUE INDEX CONCURRENTLY for online-safe application (no writer locks).

Revision ID: 012_unique_cognito_sub
Revises: 011_tenants_table
Create Date: 2026-05-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "012_unique_cognito_sub"
down_revision: str | None = "011_tenants_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create unique partial index on users.cognito_sub for non-NULL values."""
    # Use CONCURRENTLY to avoid locking the users table during index creation.
    # Note: CONCURRENTLY requires the migration to NOT run inside a transaction.
    op.execute(
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
        "uq_users_cognito_sub ON users (cognito_sub) "
        "WHERE cognito_sub IS NOT NULL"
    )


def downgrade() -> None:
    """Drop the unique partial index on users.cognito_sub."""
    op.execute("DROP INDEX IF EXISTS uq_users_cognito_sub")
