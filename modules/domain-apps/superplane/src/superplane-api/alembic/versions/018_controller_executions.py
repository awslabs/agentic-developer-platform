"""Controller assignments published only by the trusted execution service."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "018_controller_executions"
down_revision = "017_add_workspace_bootstrap_reservations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "controller_executions",
        sa.Column("operation_id", sa.String(255), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("controller_holder", sa.String(255), nullable=False),
        sa.Column("assignment", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_controller_executions_org_id", "controller_executions", ["org_id"]
    )

    op.create_table(
        "controller_provider_requests",
        sa.Column("idempotency_key", sa.String(255), primary_key=True),
        sa.Column("operation_id", sa.String(255), nullable=False),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("workspace_id", sa.String(255), nullable=False),
        sa.Column("cluster_name", sa.String(255), nullable=False),
        sa.Column("operation_kind", sa.String(255), nullable=False),
        sa.Column("request_id", sa.String(255), nullable=True),
    )
    op.create_table(
        "controller_capacity",
        sa.Column("org_id", sa.String(255), primary_key=True),
        sa.Column("workspace_id", sa.String(255), primary_key=True),
        sa.Column("cluster_name", sa.String(255), primary_key=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "state IN ('creating', 'retiring', 'retired')",
            name="ck_controller_capacity_state",
        ),
    )

    op.create_table(
        "controller_execution_accounting",
        sa.Column("operation_id", sa.String(255), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("observation", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("controller_execution_accounting")
    op.drop_table("controller_capacity")
    op.drop_table("controller_provider_requests")
    op.drop_table("controller_executions")
