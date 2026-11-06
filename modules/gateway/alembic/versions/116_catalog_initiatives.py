"""Add append-only Initiative and Contribution catalog records.

Revision ID: 116_catalog_initiatives
Revises: 115_product_onboarding_sessions
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "116_catalog_initiatives"
down_revision = "115_product_onboarding_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "catalog_initiative_versions",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("source_connection_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0"),
    )
    op.create_table(
        "catalog_initiative_heads",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("source_connection_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_connection_id", "id", "revision"],
            ["catalog_initiative_versions.tenant_id", "catalog_initiative_versions.source_connection_id",
             "catalog_initiative_versions.id", "catalog_initiative_versions.revision"],
        ),
    )
    op.create_table(
        "catalog_contribution_versions",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("source_connection_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("initiative_source", sa.String(255), nullable=False),
        sa.Column("initiative_id", sa.String(255), nullable=False),
        sa.Column("initiative_revision", sa.BigInteger(), nullable=False),
        sa.Column("product_source", sa.String(255), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column("work_links", JSONB(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("supersedes_revision", sa.BigInteger()),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "initiative_source", "initiative_id", "initiative_revision"],
            ["catalog_initiative_versions.tenant_id", "catalog_initiative_versions.source_connection_id",
             "catalog_initiative_versions.id", "catalog_initiative_versions.revision"],
        ),
    )
    op.create_index("ix_catalog_contribution_project", "catalog_contribution_versions", ["tenant_id", "project_id", "id"])
    op.create_table(
        "catalog_contribution_heads",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("source_connection_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("initiative_source", sa.String(255), nullable=False),
        sa.Column("initiative_id", sa.String(255), nullable=False),
        sa.Column("product_source", sa.String(255), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_connection_id", "id", "revision"],
            ["catalog_contribution_versions.tenant_id", "catalog_contribution_versions.source_connection_id",
             "catalog_contribution_versions.id", "catalog_contribution_versions.revision"],
        ),
        sa.UniqueConstraint("tenant_id", "initiative_source", "initiative_id", "product_source", "product_id", "project_id",
                            name="uq_catalog_contribution_tuple"),
    )


def downgrade():
    raise RuntimeError("Catalog history and receipts must be retained on rollback")
