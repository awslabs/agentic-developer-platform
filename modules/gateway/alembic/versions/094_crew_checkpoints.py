"""Add Crew checkpoint metadata and one pointer to the existing owner inventory."""

import sqlalchemy as sa

from alembic import op

revision = "094_crew_checkpoints"
down_revision = "094_credential_agent_consent"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("crew_executors", sa.Column("owner_key", sa.String(52)))
    op.add_column("crew_executors", sa.Column("latest_verified_checkpoint_id", sa.String(36)))
    op.create_index("uq_crew_executors_owner_key", "crew_executors", ["owner_key"], unique=True)
    op.create_table(
        "crew_checkpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("executor_id", sa.String(36), sa.ForeignKey("crew_executors.id"), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("owner_key", sa.String(52), nullable=False),
        sa.Column("target_kind", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("predecessor_id", sa.String(36)),
        sa.Column("status", sa.String(16), nullable=False, server_default="uploading"),
        sa.Column("archive_bucket", sa.String(255)),
        sa.Column("archive_key", sa.String(1024)),
        sa.Column("archive_version_id", sa.String(255)),
        sa.Column("archive_sha256", sa.String(64)),
        sa.Column("archive_size", sa.BigInteger()),
        sa.Column("manifest_bucket", sa.String(255)),
        sa.Column("manifest_key", sa.String(1024)),
        sa.Column("manifest_version_id", sa.String(255)),
        sa.Column("manifest_sha256", sa.String(64)),
        sa.Column("manifest_size", sa.BigInteger()),
        sa.Column("crew_version", sa.String(64)),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("failure_class", sa.String(64)),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retention_until", sa.DateTime(timezone=True)),
        sa.Column("restore_pins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("restore_token_hash", sa.String(64)),
        sa.CheckConstraint("generation > 0 AND sequence > 0", name="ck_crew_checkpoints_sequence"),
        sa.CheckConstraint("status IN ('uploading', 'verified', 'failed', 'deleting', 'deleted')", name="ck_crew_checkpoints_status"),
        sa.UniqueConstraint("tenant_id", "user_id", "generation", "idempotency_key", name="uq_crew_checkpoint_retry"),
        sa.UniqueConstraint("tenant_id", "user_id", "sequence", name="uq_crew_checkpoint_sequence"),
    )
    op.create_index("ix_crew_checkpoints_executor_status", "crew_checkpoints", ["executor_id", "status"])


def downgrade():
    op.drop_index("ix_crew_checkpoints_executor_status", table_name="crew_checkpoints")
    op.drop_table("crew_checkpoints")
    op.drop_index("uq_crew_executors_owner_key", table_name="crew_executors")
    op.drop_column("crew_executors", "latest_verified_checkpoint_id")
    op.drop_column("crew_executors", "owner_key")
