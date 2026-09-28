"""Register separately approved lifecycle control operations without moving bootstrap."""

from alembic import op
import sqlalchemy as sa

revision = "029_lifecycle_control_registry"
down_revision = "028_merge_lifecycle_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workspace_lifecycle_control_operations",
        sa.Column("operation_id", sa.String(255), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("workspace_id", sa.String(255), nullable=False),
        sa.Column("source_bootstrap_operation_id", sa.String(255), nullable=False),
        sa.Column("phase", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("allocation_id", sa.String(255), nullable=False),
        sa.Column("original_allocation_id", sa.String(255), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "org_id",
            "workspace_id",
            "source_bootstrap_operation_id",
            "phase",
            "request_id",
            name="uq_lifecycle_control_request",
        ),
        sa.CheckConstraint("phase='prepare-retirement-access'"),
        sa.CheckConstraint("allocation_id<>original_allocation_id"),
        sa.CheckConstraint("plan_digest ~ '^[a-f0-9]{64}$'"),
    )


def downgrade():
    op.drop_table("workspace_lifecycle_control_operations")
