"""Register workload operations independently from workspace lifecycle ownership."""

from alembic import op
import sqlalchemy as sa

revision = "031_controller_deployment_registry"
down_revision = "030_merge_audit_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "deployments", sa.Column("controller_request_payload", sa.Text(), nullable=True)
    )
    op.add_column(
        "deployments", sa.Column("controller_approval_id", sa.String(64), nullable=True)
    )
    op.create_table(
        "controller_deployment_operations",
        sa.Column("operation_id", sa.String(255), primary_key=True),
        sa.Column("deployment_id", sa.String(36), nullable=False),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("workspace_id", sa.String(255), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("allocation_id", sa.String(255), nullable=False),
        sa.Column("source_operation_id", sa.String(255), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("target_sha256", sa.String(64), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "org_id",
            "workspace_id",
            "deployment_id",
            "action",
            name="uq_controller_deployment_action",
        ),
        sa.UniqueConstraint(
            "org_id",
            "workspace_id",
            "request_id",
            name="uq_controller_deployment_request",
        ),
        sa.CheckConstraint("action IN ('provision','teardown')"),
        sa.CheckConstraint(
            "(action='provision' AND source_operation_id='') OR (action='teardown' AND source_operation_id<>'')"
        ),
        sa.CheckConstraint("plan_digest ~ '^[a-f0-9]{64}$'"),
        sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'"),
        sa.CheckConstraint("target_sha256 ~ '^[a-f0-9]{64}$'"),
    )


def downgrade():
    op.drop_table("controller_deployment_operations")
    op.drop_column("deployments", "controller_approval_id")
    op.drop_column("deployments", "controller_request_payload")
