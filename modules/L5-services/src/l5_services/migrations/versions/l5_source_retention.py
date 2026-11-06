"""Keep immutable source artifacts for ongoing provider runs.

Revision ID: l5_source_retention
Revises: l5_wa_cost_merge
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from l5_services.migrations.retained_downgrade import require_empty

revision = "l5_source_retention"
down_revision = "l5_wa_cost_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "l5_service_source_retention",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("artifact_id", sa.String(255), primary_key=True),
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("artifact_ref", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("required_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("last_evidence", sa.JSON().with_variant(JSONB(), "postgresql")),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
    )
    op.create_index("ix_l5_source_retention_artifact", "l5_service_source_retention", ["tenant_id", "artifact_id"])


def downgrade():
    require_empty("l5_service_source_retention")
    op.drop_index("ix_l5_source_retention_artifact", table_name="l5_service_source_retention")
    op.drop_table("l5_service_source_retention")
