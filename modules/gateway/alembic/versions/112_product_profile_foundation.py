"""Add tenant-scoped Product and Project profile foundation.

Revision ID: 112_product_profile_foundation
Revises: 111_oauth_reconnect_resources
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "112_product_profile_foundation"
down_revision = "111_oauth_reconnect_resources"
branch_labels = None
depends_on = None


def scoped_id(length=36):
    return sa.Column("id", sa.String(length), primary_key=True)


def tenant_id():
    return sa.Column("tenant_id", sa.String(255), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True)


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade():
    op.create_table(
        "products",
        tenant_id(),
        scoped_id(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("intended_users", JSONB(), nullable=False),
        sa.Column("owner_user_id", sa.String(255), nullable=False),
        sa.Column("team_id", sa.String(255)),
        sa.Column("starting_point", sa.String(20), nullable=False),
        sa.Column("lifecycle_status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("domain", sa.String(200)),
        sa.Column("goals", JSONB(), nullable=False, server_default="[]"),
        sa.Column("provenance", JSONB(), nullable=False, server_default="[]"),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1.0"),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(255), nullable=False),
        *timestamps(),
        sa.CheckConstraint("revision >= 1"),
    )
    op.create_index("ix_products_created", "products", ["tenant_id", "created_at", "id"])
    op.create_index("ix_products_owner", "products", ["tenant_id", "owner_user_id", "id"])
    op.create_index("ix_products_team", "products", ["tenant_id", "team_id", "id"])
    op.create_table(
        "projects",
        tenant_id(),
        scoped_id(255),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("owner_user_id", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="registered"),
        sa.Column("configuration", JSONB(), nullable=False, server_default="{}"),
        sa.Column("provenance", JSONB(), nullable=False, server_default="[]"),
        sa.Column("execution_policy_ref", JSONB()),
        sa.Column("budget_policy_ref", JSONB()),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        *timestamps(),
    )
    op.create_index("ix_projects_created", "projects", ["tenant_id", "created_at", "id"])
    op.create_table(
        "product_project_bindings",
        tenant_id(),
        scoped_id(),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("relationship_revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("supersedes_revision", sa.BigInteger()),
        sa.Column("mapping_version", sa.String(64), nullable=False, server_default="profile-registry-1"),
        sa.Column("source_connection_id", sa.String(255), nullable=False, server_default="adp.profile"),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("provenance", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
        sa.ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from"),
    )
    op.create_index("ix_bindings_product", "product_project_bindings", ["tenant_id", "product_id", "valid_to", "project_id"])
    op.create_index(
        "uq_bindings_current_project",
        "product_project_bindings",
        ["tenant_id", "project_id"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.create_table(
        "product_operations",
        tenant_id(),
        scoped_id(),
        sa.Column("product_id", sa.String(36)),
        sa.Column("project_id", sa.String(255)),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="succeeded"),
        sa.Column("request", JSONB(), nullable=False),
        sa.Column("receipt", JSONB()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
        sa.ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        sa.UniqueConstraint("tenant_id", "actor_id", "operation", "key_hash"),
    )
    op.create_table(
        "product_changes",
        tenant_id(),
        scoped_id(),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(255)),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1.0"),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("changed_paths", JSONB(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("source_revisions", JSONB(), nullable=False, server_default="[]"),
        sa.Column("relationship_versions", JSONB(), nullable=False, server_default="[]"),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
        sa.ForeignKeyConstraint(["tenant_id", "operation_id"], ["product_operations.tenant_id", "product_operations.id"]),
        sa.UniqueConstraint("tenant_id", "product_id", "revision"),
    )


def downgrade():
    for table in ("product_changes", "product_operations", "product_project_bindings", "projects", "products"):
        op.drop_table(table)
