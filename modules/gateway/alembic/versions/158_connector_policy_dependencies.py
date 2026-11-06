"""Pin context policy digests and versioned cell-local dependency metadata."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "158_connector_policy_deps"
down_revision = "157_connector_operation_contexts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("connector_operation_contexts", sa.Column("policy_digest", sa.String(64), nullable=True))
    op.execute("UPDATE connector_operation_contexts SET policy_digest = '' WHERE policy_digest IS NULL")
    op.alter_column("connector_operation_contexts", "policy_digest", nullable=False)
    op.create_table(
        "connector_evidence_dependencies",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("cell_id", sa.String(36), nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("identity", JSONB(), nullable=False),
        sa.Column("policy_refs", JSONB(), nullable=False),
        sa.Column("depends_on", JSONB(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "cell_id"], ["connector_cells.tenant_id", "connector_cells.project_id", "connector_cells.id"]
        ),
        sa.CheckConstraint("revision >= 1"),
        sa.CheckConstraint("state IN ('active', 'revoked')"),
        sa.UniqueConstraint("tenant_id", "project_id", "cell_id", "identity_hash", "revision"),
    )
    op.create_index("ix_connector_dependency_cell", "connector_evidence_dependencies", ["tenant_id", "project_id", "cell_id", "state"])


def downgrade():
    op.drop_index("ix_connector_dependency_cell", table_name="connector_evidence_dependencies")
    op.drop_table("connector_evidence_dependencies")
    op.drop_column("connector_operation_contexts", "policy_digest")
