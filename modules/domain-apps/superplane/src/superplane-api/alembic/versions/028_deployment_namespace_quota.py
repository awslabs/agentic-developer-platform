"""Record deployment namespaces without inventing ownership for legacy rows."""

from alembic import op
import sqlalchemy as sa

revision = "028_deployment_namespace_quota"
down_revision = "027_cli_bootstrap_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("deployments", sa.Column("namespace", sa.String(255), nullable=True))
    op.create_index(
        "ix_deployments_workspace_status", "deployments", ["workspace_id", "status"]
    )


def downgrade():
    op.drop_index("ix_deployments_workspace_status", table_name="deployments")
    op.drop_column("deployments", "namespace")
