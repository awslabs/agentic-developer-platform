"""Add github_installation_ids and cognito_client_ids to organizations.

Issue #375: tenant-identity Phase A — additive schema for identity-index.

Revision ID: 005_identity_columns
Revises: 004_vault_schema
Create Date: 2026-05-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "005_identity_columns"
down_revision: str | None = "004_vault_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add identity columns + GIN indexes. Idempotent across re-runs.

    Inspect the live schema first and skip operations that would fail on a
    partial prior apply (e.g. columns committed outside a transaction that
    alembic's version bump then rolled back).
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("organizations")}
    existing_indexes = {i["name"] for i in inspector.get_indexes("organizations")}

    if "github_installation_ids" not in existing_cols:
        op.add_column(
            "organizations",
            sa.Column(
                "github_installation_ids",
                JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )
    if "cognito_client_ids" not in existing_cols:
        op.add_column(
            "organizations",
            sa.Column(
                "cognito_client_ids",
                JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )

    # GIN indexes for containment queries
    if "ix_organizations_github_installation_ids" not in existing_indexes:
        op.create_index(
            "ix_organizations_github_installation_ids",
            "organizations",
            ["github_installation_ids"],
            postgresql_using="gin",
        )
    if "ix_organizations_cognito_client_ids" not in existing_indexes:
        op.create_index(
            "ix_organizations_cognito_client_ids",
            "organizations",
            ["cognito_client_ids"],
            postgresql_using="gin",
        )


def downgrade() -> None:
    """Remove identity columns from organizations table."""
    op.drop_index("ix_organizations_cognito_client_ids", table_name="organizations")
    op.drop_index("ix_organizations_github_installation_ids", table_name="organizations")
    op.drop_column("organizations", "cognito_client_ids")
    op.drop_column("organizations", "github_installation_ids")
