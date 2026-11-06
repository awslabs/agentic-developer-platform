"""Persist opaque cell-bound operation handles on the gateway lineage."""

import sqlalchemy as sa

from alembic import op

revision = "157_connector_operation_contexts"
down_revision = "156_connector_cell_authority"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_connector_grant_project_identity", "connector_collection_grants", ["tenant_id", "project_id", "id"])
    op.create_table(
        "connector_operation_contexts",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("grant_id", sa.String(36), nullable=False),
        sa.Column("cell_id", sa.String(36), nullable=False),
        sa.Column("delegation_id", sa.String(36), nullable=False),
        sa.Column("delegation_revision", sa.BigInteger(), nullable=False),
        sa.Column("consumer", sa.String(255), nullable=False),
        sa.Column("operation_id", sa.String(255), nullable=False),
        sa.Column("attempt_id", sa.String(255), nullable=False),
        sa.Column("lease_fence", sa.BigInteger(), nullable=False),
        sa.Column("source_generation", sa.BigInteger(), nullable=False),
        sa.Column("routing_revision", sa.BigInteger(), nullable=False),
        sa.Column("policy_revision", sa.BigInteger(), nullable=False),
        sa.Column("context_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "grant_id"],
            ["connector_collection_grants.tenant_id", "connector_collection_grants.project_id", "connector_collection_grants.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "cell_id"], ["connector_cells.tenant_id", "connector_cells.project_id", "connector_cells.id"]
        ),
        sa.UniqueConstraint("tenant_id", "consumer", "operation_id", "attempt_id"),
        sa.CheckConstraint("source_generation >= 1 AND routing_revision >= 1 AND policy_revision >= 1 AND lease_fence >= 1"),
    )
    op.create_index("ix_connector_context_grant", "connector_operation_contexts", ["tenant_id", "grant_id", "expires_at"])


def downgrade():
    op.drop_index("ix_connector_context_grant", table_name="connector_operation_contexts")
    op.drop_table("connector_operation_contexts")
    op.drop_constraint("uq_connector_grant_project_identity", "connector_collection_grants", type_="unique")
