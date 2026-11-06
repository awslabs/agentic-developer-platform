"""Create the additive Portfolio catalog tables.

Revision ID: 114_catalog_portfolios
Revises: 113_product_scope_guard
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "114_catalog_portfolios"
down_revision = "113_product_scope_guard"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "catalog_portfolio_versions",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("source_connection_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0"),
    )
    op.create_table(
        "catalog_portfolio_heads",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("source_connection_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_connection_id", "id", "revision"],
            ["catalog_portfolio_versions.tenant_id", "catalog_portfolio_versions.source_connection_id",
             "catalog_portfolio_versions.id", "catalog_portfolio_versions.revision"],
        ),
    )
    op.create_table(
        "catalog_membership_versions",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("relationship_id", sa.String(255), primary_key=True),
        sa.Column("relationship_revision", sa.BigInteger(), primary_key=True),
        sa.Column("portfolio_source", sa.String(255), nullable=False),
        sa.Column("portfolio_id", sa.String(255), nullable=False),
        sa.Column("portfolio_revision", sa.BigInteger(), nullable=False),
        sa.Column("product_source", sa.String(255), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("product_revision", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("source_connection_id", sa.String(255), nullable=False),
        sa.Column("mapping_version", sa.String(64), nullable=False),
        sa.Column("source_refs", JSONB(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("supersedes_revision", sa.BigInteger()),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("relationship_revision > 0"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "portfolio_source", "portfolio_id", "portfolio_revision"],
            ["catalog_portfolio_versions.tenant_id", "catalog_portfolio_versions.source_connection_id",
             "catalog_portfolio_versions.id", "catalog_portfolio_versions.revision"],
        ),
    )
    op.create_index("ix_catalog_membership_product", "catalog_membership_versions",
                    ["tenant_id", "product_source", "product_id"])
    op.create_table(
        "catalog_membership_heads",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("relationship_id", sa.String(255), primary_key=True),
        sa.Column("relationship_revision", sa.BigInteger(), nullable=False),
        sa.Column("portfolio_source", sa.String(255), nullable=False),
        sa.Column("portfolio_id", sa.String(255), nullable=False),
        sa.Column("product_source", sa.String(255), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "relationship_id", "relationship_revision"],
            ["catalog_membership_versions.tenant_id", "catalog_membership_versions.relationship_id",
             "catalog_membership_versions.relationship_revision"],
        ),
        sa.UniqueConstraint("tenant_id", "portfolio_source", "portfolio_id", "product_source", "product_id",
                            name="uq_catalog_membership_pair"),
    )
    op.create_table(
        "catalog_changes",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("source_connection_id", sa.String(255), nullable=False),
        sa.Column("record_type", sa.String(32), nullable=False),
        sa.Column("record_id", sa.String(255), nullable=False),
        sa.Column("native_record_revision", sa.String(255), nullable=False),
        sa.Column("relationship_revision", sa.BigInteger()),
        sa.Column("mapping_version", sa.String(64), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("source_refs", JSONB(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_catalog_changes_record", "catalog_changes",
                    ["tenant_id", "record_type", "record_id", "recorded_at"])
    op.create_table(
        "catalog_mutation_receipts",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("actor_id", sa.String(255), primary_key=True),
        sa.Column("operation", sa.String(80), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("outcome", JSONB(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("Catalog history and receipts must be retained on rollback")
