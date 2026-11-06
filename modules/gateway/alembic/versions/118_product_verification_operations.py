"""Bound Product verification attempts with recoverable short leases.

Revision ID: 118_product_verification_ops
Revises: 117_product_source_bindings
"""

import sqlalchemy as sa

from alembic import op

revision = "118_product_verification_ops"
down_revision = "117_product_source_bindings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("product_operations", sa.Column("binding_id", sa.String(36)))
    op.add_column("product_operations", sa.Column("binding_generation", sa.BigInteger()))
    op.add_column("product_operations", sa.Column("expected_revision", sa.BigInteger()))
    op.add_column("product_operations", sa.Column("lease_owner", sa.String(64)))
    op.add_column("product_operations", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column("product_operations", sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("product_operations", sa.Column("retry_at", sa.DateTime(timezone=True)))
    op.create_foreign_key(
        "fk_product_operation_source_binding", "product_operations", "product_source_bindings",
        ["tenant_id", "binding_id"], ["tenant_id", "id"],
    )
    op.create_check_constraint(
        "ck_product_operation_lease_pair", "product_operations", "(lease_owner IS NULL) = (lease_until IS NULL)",
    )
    op.create_check_constraint(
        "ck_product_operation_attempts", "product_operations", "attempt_count >= 0 AND attempt_count <= 10",
    )
    op.create_index("ix_product_operation_recovery", "product_operations", ["state", "retry_at", "lease_until"])


def downgrade():
    op.drop_index("ix_product_operation_recovery", table_name="product_operations")
    op.drop_constraint("ck_product_operation_attempts", "product_operations", type_="check")
    op.drop_constraint("ck_product_operation_lease_pair", "product_operations", type_="check")
    op.drop_constraint("fk_product_operation_source_binding", "product_operations", type_="foreignkey")
    for column in ("retry_at", "attempt_count", "lease_until", "lease_owner", "expected_revision", "binding_generation", "binding_id"):
        op.drop_column("product_operations", column)
