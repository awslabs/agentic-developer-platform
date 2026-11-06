"""Persist Product source selections without credential material.

Revision ID: 117_product_source_bindings
Revises: 116_catalog_initiatives
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "117_product_source_bindings"
down_revision = "116_catalog_initiatives"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "product_source_bindings",
        sa.Column("tenant_id", sa.String(255), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("installation_id", sa.String(36), sa.ForeignKey("channel_tenant_map.id")),
        sa.Column("credential_id", sa.String(36), sa.ForeignKey("user_credentials.id")),
        sa.Column("namespace", sa.String(512), nullable=False),
        sa.Column("native_id", sa.String(512), nullable=False),
        sa.Column("desired", JSONB(), nullable=False),
        sa.Column("observed", JSONB()),
        sa.Column("status", sa.String(20), nullable=False, server_default="unverified"),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("unlinked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        sa.CheckConstraint("(installation_id IS NOT NULL) != (credential_id IS NOT NULL)"),
        sa.CheckConstraint("(status = 'unlinked') = (unlinked_at IS NOT NULL)"),
        sa.CheckConstraint("status IN ('unverified', 'healthy', 'degraded', 'revoked', 'unlinked')"),
        sa.CheckConstraint("generation >= 1 AND revision >= 1"),
    )
    op.create_index("ix_source_bindings_project", "product_source_bindings", ["tenant_id", "project_id", "status", "id"])
    op.create_index("ix_source_bindings_installation", "product_source_bindings", ["installation_id"])
    op.create_index("ix_source_bindings_credential", "product_source_bindings", ["credential_id"])
    op.create_index(
        "uq_source_bindings_active_identity", "product_source_bindings",
        ["tenant_id", "project_id", "kind", "provider", "namespace", "native_id"],
        unique=True, postgresql_where=sa.text("unlinked_at IS NULL"),
    )


def downgrade():
    op.drop_index("uq_source_bindings_active_identity", table_name="product_source_bindings")
    op.drop_index("ix_source_bindings_credential", table_name="product_source_bindings")
    op.drop_index("ix_source_bindings_installation", table_name="product_source_bindings")
    op.drop_index("ix_source_bindings_project", table_name="product_source_bindings")
    op.drop_table("product_source_bindings")
