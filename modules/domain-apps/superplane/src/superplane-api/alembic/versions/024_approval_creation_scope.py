"""Retain the authority scope of approvals issued before workspace creation."""

import sqlalchemy as sa
from alembic import op

revision = "024_approval_creation_scope"
down_revision = "023_operation_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "operation_approvals",
        sa.Column(
            "organization_scope",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("operation_settlement_receipts", "receipt_id", type_=sa.String(128))


def downgrade() -> None:
    op.alter_column("operation_settlement_receipts", "receipt_id", type_=sa.String(64))
    op.drop_column("operation_approvals", "organization_scope")
