"""Add CHECK constraint on user_identities.provider column.

Issue #537: Identity projection redesign — enforce provider values at DB level.

Revision ID: 009_provider_check_constraint
Revises: 008_magic_link
Create Date: 2026-05-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "009_provider_check_constraint"
down_revision: str | None = "008_magic_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must match src/shared/identity/providers.py::SUPPORTED_PROVIDERS
SUPPORTED_PROVIDERS = ("cognito", "github", "slack", "teams", "discord", "email", "whatsapp")


def upgrade() -> None:
    # Only add CHECK constraint on PostgreSQL (skip for SQLite in tests)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        providers_list = ", ".join(f"'{p}'" for p in SUPPORTED_PROVIDERS)
        op.execute(
            sa.text(
                f"ALTER TABLE user_identities "
                f"ADD CONSTRAINT ck_user_identities_provider "
                f"CHECK (provider IN ({providers_list}))"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text("ALTER TABLE user_identities DROP CONSTRAINT IF EXISTS ck_user_identities_provider")
        )
