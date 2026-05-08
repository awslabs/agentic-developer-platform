"""Create tenant_access_requests table for onboarding flow.

Issue #538: Onboarding flow — GitHub sign-in to tenant + user creation.

Revision ID: 010_tenant_access_requests
Revises: 009_provider_check_constraint
Create Date: 2026-05-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "010_tenant_access_requests"
down_revision: str | None = "009_provider_check_constraint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_access_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cognito_sub", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_user_id", sa.String(255), nullable=False),
        sa.Column("proposed_tenant_id", sa.String(255), nullable=False),
        sa.Column("target_login", sa.String(255), nullable=False),
        sa.Column("motivation", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Indexes — partial unique index only on PostgreSQL (SQLite lacks WHERE)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "CREATE UNIQUE INDEX ux_tenant_access_requests_pending "
                "ON tenant_access_requests(provider, provider_user_id) "
                "WHERE status = 'pending'"
            )
        )
        op.execute(
            sa.text(
                "CREATE INDEX ix_tenant_access_requests_proposed_tenant_id "
                "ON tenant_access_requests(proposed_tenant_id) "
                "WHERE status = 'pending'"
            )
        )
    else:
        # SQLite fallback: plain index (no partial support)
        op.create_index(
            "ix_tenant_access_requests_provider_user",
            "tenant_access_requests",
            ["provider", "provider_user_id"],
        )
        op.create_index(
            "ix_tenant_access_requests_proposed_tenant_id",
            "tenant_access_requests",
            ["proposed_tenant_id"],
        )


def downgrade() -> None:
    op.drop_table("tenant_access_requests")
