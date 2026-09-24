"""Record deployment namespaces without inventing ownership for legacy rows."""

from alembic import op
import sqlalchemy as sa

revision = "022_deployment_namespace_quota"
down_revision = "021_deployment_identity"
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
