"""Persist request-bound approvals and idempotent budget settlement receipts."""

import sqlalchemy as sa
from alembic import op

revision = "023_operation_governance"
down_revision = "022_merge_budget_cli"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_approvals",
        sa.Column("approval_id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("workspace_id", sa.String(255), nullable=False),
        sa.Column("requester", sa.String(255), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("request_payload", sa.Text(), nullable=False),
        sa.Column("approvers_json", sa.Text(), nullable=False),
        sa.Column("max_resource_units", sa.BigInteger(), nullable=False),
        sa.Column("max_runtime_seconds", sa.BigInteger(), nullable=False),
        sa.Column("max_cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("result", sa.String(32)),
        sa.Column("decided_by", sa.String(255)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "org_id",
            "workspace_id",
            "requester",
            "plan_digest",
            name="uq_operation_approval_request",
        ),
    )
    op.create_table(
        "operation_settlement_receipts",
        sa.Column("receipt_id", sa.String(64), primary_key=True),
        sa.Column("operation_id", sa.String(255), nullable=False, unique=True),
        sa.Column("reservation_id", sa.String(128), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("operation_settlement_receipts")
    op.drop_table("operation_approvals")
