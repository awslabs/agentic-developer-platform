"""Register gateway-owned connector policy, cells and explicit grants."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "156_connector_cell_authority"
down_revision = "155_l5_publication_retry"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_source_binding_project_identity", "product_source_bindings", ["tenant_id", "project_id", "id"])
    op.create_table(
        "connector_policy_realms",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("project_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("descriptor_digest", sa.String(64), nullable=False),
        sa.Column("descriptor", JSONB(), nullable=False),
        sa.Column("source_policy_ref", sa.String(255), nullable=False),
        sa.Column("evidence_policy_ref", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        sa.CheckConstraint("revision >= 1"),
        sa.CheckConstraint("state IN ('active', 'retired')"),
    )
    op.create_table(
        "connector_cells",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("realm_id", sa.String(36), nullable=False),
        sa.Column("realm_revision", sa.BigInteger(), nullable=False),
        sa.Column("database_ref", sa.String(255), nullable=False),
        sa.Column("role_ref", sa.String(255), nullable=False),
        sa.Column("object_prefix", sa.String(512), nullable=False),
        sa.Column("workload_id", sa.String(255), nullable=False),
        sa.Column("audience", sa.String(255), nullable=False),
        sa.Column("routing_revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "realm_id", "realm_revision"],
            [
                "connector_policy_realms.tenant_id", "connector_policy_realms.project_id",
                "connector_policy_realms.id", "connector_policy_realms.revision",
            ],
        ),
        sa.UniqueConstraint("tenant_id", "project_id", "id"),
        sa.UniqueConstraint("tenant_id", "project_id", "realm_id", "realm_revision"),
        sa.CheckConstraint("routing_revision >= 1"),
        sa.CheckConstraint("state IN ('active', 'retired')"),
    )
    op.create_table(
        "connector_collection_grants",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("binding_id", sa.String(36), nullable=False),
        sa.Column("binding_generation", sa.BigInteger(), nullable=False),
        sa.Column("cell_id", sa.String(36), nullable=False),
        sa.Column("consumer", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("source_selections", JSONB(), nullable=False),
        sa.Column("granting_actor_id", sa.String(255), nullable=False),
        sa.Column("policy_revision", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "binding_id"],
            ["product_source_bindings.tenant_id", "product_source_bindings.project_id", "product_source_bindings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "cell_id"],
            ["connector_cells.tenant_id", "connector_cells.project_id", "connector_cells.id"],
        ),
        sa.CheckConstraint("binding_generation >= 1 AND policy_revision >= 1"),
        sa.CheckConstraint("state IN ('active', 'revoked')"),
    )
    op.create_index("ix_connector_grants_binding", "connector_collection_grants", ["tenant_id", "binding_id", "state"])


def downgrade():
    op.drop_index("ix_connector_grants_binding", table_name="connector_collection_grants")
    op.drop_table("connector_collection_grants")
    op.drop_table("connector_cells")
    op.drop_table("connector_policy_realms")
    op.drop_constraint("uq_source_binding_project_identity", "product_source_bindings", type_="unique")
