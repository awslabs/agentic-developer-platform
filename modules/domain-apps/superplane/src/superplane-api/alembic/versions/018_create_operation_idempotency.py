"""Make workspace and deployment creation idempotent per organization.

Revision ID: 018_create_operation_idempotency
Revises: 017_unique_credential_reference
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "018_create_operation_idempotency"
down_revision = "017_unique_credential_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("workspaces", "deployments"):
        op.add_column(
            table,
            sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.add_column(
            table,
            sa.Column("operation_request_json", sa.Text(), nullable=True),
        )
    op.create_unique_constraint(
        "uq_workspaces_org_operation",
        "workspaces",
        ["org_id", "operation_id"],
    )
    op.create_unique_constraint(
        "uq_deployments_org_operation",
        "deployments",
        ["org_id", "operation_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_deployments_org_operation", "deployments", type_="unique"
    )
    op.drop_constraint("uq_workspaces_org_operation", "workspaces", type_="unique")
    for table in ("deployments", "workspaces"):
        op.drop_column(table, "operation_request_json")
        op.drop_column(table, "operation_id")
