"""Create tenants table (thin forward-looking layer over organizations).

Issue #538: Onboarding flow — tenants.id IS organizations.id (1:1, same string).

Revision ID: 011_tenants_table
Revises: 010_tenant_access_requests
Create Date: 2026-05-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "011_tenants_table"
down_revision: str | None = "010_tenant_access_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("shared_app_ref", sa.String(512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # FK to organizations — only on PostgreSQL (SQLite FK enforcement is limited)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_tenants_organizations",
            "tenants",
            "organizations",
            ["id"],
            ["id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint("fk_tenants_organizations", "tenants", type_="foreignkey")
    op.drop_table("tenants")
