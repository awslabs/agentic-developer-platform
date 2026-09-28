"""Add quota enforcement fields to workspaces.

Adds max_nodes and allowed_clouds_json columns to workspaces table.
The quotas_json column already exists on both organizations and workspaces,
but we add explicit columns for fast indexed queries during enforcement.

Revision ID: 007_add_quota_fields
Revises: 006_add_cognito_sub
"""

from alembic import op
import sqlalchemy as sa

revision = "007_add_quota_fields"
down_revision = "006_add_cognito_sub"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add max_nodes column to workspaces for direct enforcement
    op.add_column(
        "workspaces",
        sa.Column("budget_max_nodes", sa.Integer(), nullable=True),
    )
    # Add allowed_clouds column as a JSON array for cloud restriction
    op.add_column(
        "workspaces",
        sa.Column("allowed_clouds_json", sa.Text(), nullable=True),
    )

    # Add index on workspace status for faster quota enforcement queries
    op.create_index(
        "ix_workspaces_status_org",
        "workspaces",
        ["org_id", "status"],
    )

    # Add index on nodes for faster GPU counting
    op.create_index(
        "ix_nodes_cluster_status",
        "nodes",
        ["cluster_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_nodes_cluster_status", table_name="nodes")
    op.drop_index("ix_workspaces_status_org", table_name="workspaces")
    op.drop_column("workspaces", "allowed_clouds_json")
    op.drop_column("workspaces", "budget_max_nodes")
