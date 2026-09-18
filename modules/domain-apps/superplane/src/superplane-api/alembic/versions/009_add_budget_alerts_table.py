"""Add budget_alerts table — budget and GPU-limit violations per workspace.

Revision ID: 009_add_budget_alerts_table
Revises: 008_add_api_keys_table
Create Date: 2026-09-17

WHY THIS MIGRATION EXISTS (issue #5045, U13)

Same gap as the previous revision: `app/models/budget_alert.py` declares the
`budget_alerts` table and the application queries it, but no migration created it. This is
the second of the two tables that were missing from the chain.

The column set mirrors `app/models/budget_alert.py` exactly. `resolved_at` is nullable by
design — NULL means the alert is still open, which is what makes the partial index below
the useful one: the common query is "which alerts are currently unresolved for this
workspace", not "all alerts ever raised".
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "009_add_budget_alerts_table"
down_revision = "008_add_api_keys_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budget_alerts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        # budget_warning | budget_exceeded | gpu_limit_exceeded
        sa.Column("alert_type", sa.String(50), nullable=False),
        # warning | critical
        sa.Column(
            "severity", sa.String(20), nullable=False, server_default="warning"
        ),
        sa.Column("threshold_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("current_value", sa.Numeric(12, 2), nullable=True),
        sa.Column("limit_value", sa.Numeric(12, 2), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        # NULL while the alert is still open.
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Alert history for a workspace, newest first.
    op.create_index(
        "ix_budget_alerts_workspace_created",
        "budget_alerts",
        ["workspace_id", "created_at"],
    )
    # Open alerts only — the set an operator or the enforcement path actually reads.
    op.create_index(
        "ix_budget_alerts_unresolved",
        "budget_alerts",
        ["org_id"],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_budget_alerts_unresolved", table_name="budget_alerts")
    op.drop_index("ix_budget_alerts_workspace_created", table_name="budget_alerts")
    op.drop_table("budget_alerts")
