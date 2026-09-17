"""Add workspace reconciler tracking fields (US-H3).

Adds columns for failed bootstrap retry and drift detection:
- bootstrap_retry_count: number of retry attempts
- last_bootstrap_at: timestamp of last bootstrap trigger
- last_drift_check_at: timestamp of last drift detection check
- reconcile_error: last reconciliation error message

Revision ID: 006
Revises: 005
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "bootstrap_retry_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("last_bootstrap_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("last_drift_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("reconcile_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "reconcile_error")
    op.drop_column("workspaces", "last_drift_check_at")
    op.drop_column("workspaces", "last_bootstrap_at")
    op.drop_column("workspaces", "bootstrap_retry_count")
