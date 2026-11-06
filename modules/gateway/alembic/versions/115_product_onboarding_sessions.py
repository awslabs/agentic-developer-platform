"""Persist scoped onboarding sessions and operation references.

Revision ID: 115_product_onboarding_sessions
Revises: 114_catalog_portfolios
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "115_product_onboarding_sessions"
down_revision = "114_catalog_portfolios"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "product_onboarding_sessions",
        sa.Column("tenant_id", sa.String(255), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("actor_user_id", sa.String(255), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="open"),
        sa.Column("steps", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("proposals", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("rule_version", sa.String(32), nullable=False, server_default="profile-1"),
        sa.Column("last_product_revision", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
        sa.ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        sa.CheckConstraint("state IN ('open', 'waiting_external', 'completed', 'cancelled')"),
        sa.CheckConstraint("last_product_revision >= 1"),
    )
    op.create_index(
        "ix_onboarding_actor_state", "product_onboarding_sessions",
        ["tenant_id", "project_id", "actor_user_id", "state", "id"],
    )
    op.create_index(
        "uq_onboarding_active_actor", "product_onboarding_sessions",
        ["tenant_id", "product_id", "project_id", "actor_user_id"],
        unique=True, postgresql_where=sa.text("state IN ('open', 'waiting_external')"),
    )
    op.add_column("product_operations", sa.Column("session_id", sa.String(36)))
    op.create_foreign_key(
        "fk_product_operations_session", "product_operations", "product_onboarding_sessions",
        ["tenant_id", "session_id"], ["tenant_id", "id"],
    )


def downgrade():
    op.drop_constraint("fk_product_operations_session", "product_operations", type_="foreignkey")
    op.drop_column("product_operations", "session_id")
    op.drop_index("uq_onboarding_active_actor", table_name="product_onboarding_sessions")
    op.drop_index("ix_onboarding_actor_state", table_name="product_onboarding_sessions")
    op.drop_table("product_onboarding_sessions")
