"""Persist authorized workspace operation references for recovery.

Revision ID: 019_workspace_operation_state
Revises: 018_create_operation_idempotency
"""

import sqlalchemy as sa
from alembic import op

revision = "019_workspace_operation_state"
down_revision = "018_create_operation_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("provisioning_operation_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("teardown_operation_id", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "teardown_operation_id")
    op.drop_column("workspaces", "provisioning_operation_id")
