"""Keep owned and adopted FinOps setup steps independently of provider work.

Revision ID: l5_finops_setup_receipts
Revises: l5_finops_retained_scope
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "l5_finops_setup_receipts"
down_revision = "l5_finops_retained_scope"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "l5_service_setup_resource_receipts",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("resource_kind", sa.String(32), primary_key=True),
        sa.Column("ownership", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("native_resource_ref", payload),
        sa.Column("evidence_digest", sa.String(64)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["l5_service_setup_intents.tenant_id", "l5_service_setup_intents.operation_id"],
        ),
    )


def downgrade():
    op.drop_table("l5_service_setup_resource_receipts")
