"""Immutable, operation-bound workspace plan proposals."""

from alembic import op
import sqlalchemy as sa

revision = "019_lifecycle_artifacts"
down_revision = "018_bootstrap_read_tokens"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workspace_lifecycle_artifacts",
        sa.Column("artifact_id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("workspace_id", sa.String(255), nullable=False),
        sa.Column("source_operation_id", sa.String(255), nullable=False),
        sa.Column("source_job_id", sa.String(255), nullable=False),
        sa.Column("source_attempt_id", sa.String(255), nullable=False),
        sa.Column("source_payload_digest", sa.String(64), nullable=False),
        sa.Column("source_request_payload", sa.Text(), nullable=False),
        sa.Column("producer_holder", sa.String(255), nullable=False),
        sa.Column("producer_attempt_id", sa.String(255), nullable=False),
        sa.Column("producer_fence_token", sa.BigInteger(), nullable=False),
        sa.Column("request_revision", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(12), nullable=False),
        sa.Column("target_json", sa.Text(), nullable=False),
        sa.Column("parameters_json", sa.Text(), nullable=False),
        sa.Column("artifact_metadata_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("artifact_id ~ '^[a-f0-9]{64}$'"),
        sa.CheckConstraint("request_revision ~ '^[a-f0-9]{64}$'"),
        sa.CheckConstraint("account_id ~ '^[0-9]{12}$'"),
        sa.CheckConstraint("jsonb_typeof(target_json::jsonb)='object'"),
        sa.CheckConstraint("jsonb_typeof(parameters_json::jsonb)='object'"),
        sa.CheckConstraint("jsonb_typeof(artifact_metadata_json::jsonb)='object'"),
    )
    op.create_index(
        "ix_workspace_lifecycle_artifacts_source",
        "workspace_lifecycle_artifacts",
        ["org_id", "workspace_id", "source_operation_id"],
    )


def downgrade():
    op.drop_index(
        "ix_workspace_lifecycle_artifacts_source",
        table_name="workspace_lifecycle_artifacts",
    )
    op.drop_table("workspace_lifecycle_artifacts")
