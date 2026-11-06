"""Persist tenant connector admission across module disable and re-enable."""

import sqlalchemy as sa

from alembic import op

revision = "159_connector_module_admission"
down_revision = "158_connector_policy_deps"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "connector_module_admissions",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"]),
        sa.CheckConstraint("generation >= 1"),
    )
    op.add_column("connector_operation_contexts", sa.Column("module_generation", sa.BigInteger(), nullable=False, server_default="1"))


def downgrade():
    op.drop_column("connector_operation_contexts", "module_generation")
    op.drop_table("connector_module_admissions")
