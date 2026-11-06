"""Keep Security Context publication and consumer receipts independent of runs.

Revision ID: l5_publications
Revises: l5_devops_native_runs
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l5_publications"
down_revision = "l5_devops_native_runs"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "l5_service_publications",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("publication_id", sa.String(36), primary_key=True),
        sa.Column("result_id", sa.String(36), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=False),
        sa.Column("destination", sa.String(100), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("contribution_hash", sa.String(64), nullable=False),
        sa.Column("payload", payload, nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("receipt", payload),
        sa.Column("invalidation", payload),
        sa.Column("invalidation_receipt", payload),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("invalidation_attempts", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(32)),
        sa.Column("invalidation_watermark", sa.Integer(), nullable=False),
        sa.Column("applied_watermark", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "result_id"],
                                ["l5_service_results.tenant_id", "l5_service_results.result_id"]),
        sa.UniqueConstraint("tenant_id", "result_id", "result_version", "destination", "schema_version"),
    )
    op.create_index("ix_l5_publication_due", "l5_service_publications",
                    ["state", "next_due_at", "lease_until"])


def downgrade():
    op.drop_index("ix_l5_publication_due", table_name="l5_service_publications")
    op.drop_table("l5_service_publications")
