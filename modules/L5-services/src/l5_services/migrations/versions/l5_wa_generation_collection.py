"""Retain recommendation pages and the original generation identity.

Revision ID: l5_wa_generation_collection
Revises: l5_wa_generation_request
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from l5_services.migrations.retained_downgrade import require_empty

revision = "l5_wa_generation_collection"
down_revision = "l5_wa_generation_request"
branch_labels = None
depends_on = None


def upgrade():
    payload = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "l5_service_generation_collections",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("native_job_ref", payload, nullable=False),
        sa.Column("next_token", sa.String(2048)),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("recommendations", payload, nullable=False),
        sa.Column("result_id", sa.String(36)),
        sa.CheckConstraint("page_count >= 1", name="ck_l5_generation_collection_pages"),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
    )


def downgrade():
    require_empty("l5_service_generation_collections")
    op.drop_table("l5_service_generation_collections")
