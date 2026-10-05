"""Retain the exact cluster target and observed workload UID across retries.

Revision ID: 021_deployment_identity
Revises: 020_merge_workspace_cli

Existing uncertain operations have no provable target snapshot. Leave their
identity null so recovery refuses provider I/O instead of inventing provenance.
"""

from alembic import op
import sqlalchemy as sa

revision = "021_deployment_identity"
down_revision = "020_merge_workspace_cli"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployments", sa.Column("operation_target_json", sa.Text(), nullable=True)
    )
    op.add_column(
        "deployments", sa.Column("provider_uid", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("deployments", "provider_uid")
    op.drop_column("deployments", "operation_target_json")
