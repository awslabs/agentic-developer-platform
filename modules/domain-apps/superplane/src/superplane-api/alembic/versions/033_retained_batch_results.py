"""Retain bounded, original-operation batch text independently of cleanup."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "033_retained_batch_results"
down_revision = "032_batch_workload_kind"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "controller_batch_results",
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
        sa.Column(
            "deployment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("deployments.id"),
            nullable=False,
        ),
        sa.Column("allocation_id", sa.String(255), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("job_uid", sa.String(255), nullable=False),
        sa.Column("pod_uid", sa.String(255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("octet_length(content)<=16384", name="ck_batch_result_size"),
    )


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM controller_batch_results) THEN
            RAISE EXCEPTION 'Retained batch results require this schema; preserve them before rollback';
        END IF;
    END; $$""")
    op.drop_table("controller_batch_results")
