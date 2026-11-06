"""Persist later-task consumer retrieval and use without changing result revisions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from l5_services.migrations.retained_downgrade import require_empty

revision = "l5_wa_usage"
down_revision = "l5_wa_collection_exclusions"
branch_labels = None
depends_on = None


def upgrade():
    payload = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "l5_service_wa_usage",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("publication_id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(255), primary_key=True),
        sa.Column("result_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("retrieval_receipt", payload, nullable=False),
        sa.Column("usage_receipt", payload, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "publication_id"],
                                ["l5_service_publications.tenant_id", "l5_service_publications.publication_id"]),
    )


def downgrade():
    require_empty("l5_service_wa_usage")
    op.drop_table("l5_service_wa_usage")
