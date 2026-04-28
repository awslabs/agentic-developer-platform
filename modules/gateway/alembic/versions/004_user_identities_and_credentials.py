"""Add user_identities, user_credentials, and channel_tenant_map tables.

Issue #134: Vault Phase 1 — schema + secret-store substrate

Revision ID: 004_vault_schema
Revises: 003_model_pricing
Create Date: 2026-04-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "004_vault_schema"
down_revision: str | None = "003_model_pricing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create vault tables: user_identities, user_credentials, channel_tenant_map."""

    # -- user_identities --
    op.create_table(
        "user_identities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=255), nullable=False),
        # team_id is denormalised from users.team_id at insert time so team-scoped
        # queries don't need a JOIN.
        sa.Column("team_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column(
            "provider",
            sa.String(length=20),
            nullable=False,
            comment="slack | github | whatsapp | discord",
        ),
        sa.Column("provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("provider_username", sa.String(length=255), nullable=True),
        sa.Column(
            "verification_method",
            sa.String(length=20),
            nullable=False,
            comment="oauth | magic_link | admin_manual",
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])
    op.create_index("ix_user_identities_team_id", "user_identities", ["team_id"])
    op.create_index("ix_user_identities_org_id_provider", "user_identities", ["org_id", "provider"])
    op.create_index("ix_user_identities_org_id_team_id", "user_identities", ["org_id", "team_id"])
    op.create_index(
        "uq_user_identities_provider_provider_user_id",
        "user_identities",
        ["provider", "provider_user_id"],
        unique=True,
    )

    # -- user_credentials --
    op.create_table(
        "user_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=255), nullable=False),
        # team_id is denormalised from users.team_id at insert time so team-scoped
        # queries don't need a JOIN.
        sa.Column("team_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("service", sa.String(length=255), nullable=False),
        sa.Column(
            "credential_type",
            sa.String(length=20),
            nullable=False,
            comment="api_key | oauth_token | basic_auth | bearer | ssh_key | certificate | config_file",
        ),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("secret_arn", sa.String(length=512), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "uq_user_credentials_user_service_label",
        "user_credentials",
        ["user_id", "service", "label"],
        unique=True,
    )
    op.create_index("ix_user_credentials_team_id", "user_credentials", ["team_id"])
    op.create_index("ix_user_credentials_org_id_service", "user_credentials", ["org_id", "service"])
    op.create_index(
        "ix_user_credentials_org_id_team_id_service",
        "user_credentials",
        ["org_id", "team_id", "service"],
    )

    # -- channel_tenant_map --
    op.create_table(
        "channel_tenant_map",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "provider",
            sa.String(length=20),
            nullable=False,
            comment="slack | github | whatsapp | discord",
        ),
        sa.Column("provider_scope_id", sa.String(length=255), nullable=False),
        sa.Column("org_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "uq_channel_tenant_map_provider_scope",
        "channel_tenant_map",
        ["provider", "provider_scope_id"],
        unique=True,
    )


def downgrade() -> None:
    """Drop vault tables in reverse order."""
    op.drop_index("uq_channel_tenant_map_provider_scope", table_name="channel_tenant_map")
    op.drop_table("channel_tenant_map")

    op.drop_index("ix_user_credentials_org_id_team_id_service", table_name="user_credentials")
    op.drop_index("ix_user_credentials_org_id_service", table_name="user_credentials")
    op.drop_index("ix_user_credentials_team_id", table_name="user_credentials")
    op.drop_index("uq_user_credentials_user_service_label", table_name="user_credentials")
    op.drop_table("user_credentials")

    op.drop_index("uq_user_identities_provider_provider_user_id", table_name="user_identities")
    op.drop_index("ix_user_identities_org_id_team_id", table_name="user_identities")
    op.drop_index("ix_user_identities_org_id_provider", table_name="user_identities")
    op.drop_index("ix_user_identities_team_id", table_name="user_identities")
    op.drop_index("ix_user_identities_user_id", table_name="user_identities")
    op.drop_table("user_identities")
