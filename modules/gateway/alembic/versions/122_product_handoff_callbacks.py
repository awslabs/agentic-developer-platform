"""Persist scoped callback ordering and provider effect identity.

Revision ID: 122_product_handoff_callbacks
Revises: 121_merge_metering_sources
"""

import sqlalchemy as sa

from alembic import op

revision = "122_product_handoff_callbacks"
down_revision = "121_merge_metering_sources"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("product_operations", sa.Column("provider_operation_id", sa.String(512)))
    op.add_column("product_operations", sa.Column("callback_sequence", sa.BigInteger(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_product_operations_callback_sequence", "product_operations", "callback_sequence >= 0")


def downgrade():
    op.drop_constraint("ck_product_operations_callback_sequence", "product_operations", type_="check")
    op.drop_column("product_operations", "callback_sequence")
    op.drop_column("product_operations", "provider_operation_id")
