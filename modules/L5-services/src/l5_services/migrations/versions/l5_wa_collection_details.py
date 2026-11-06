"""Persist pending native recommendation details between bounded worker claims."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from l5_services.migrations.retained_downgrade import require_empty

revision = "l5_wa_collection_details"
down_revision = "l5_wa_generation_collection"
branch_labels = None
depends_on = None


def upgrade():
    payload = sa.JSON().with_variant(JSONB(), "postgresql")
    op.add_column("l5_service_generation_collections", sa.Column(
        "pending_recommendations", payload, nullable=False, server_default="[]"))


def downgrade():
    require_empty("l5_service_generation_collections")
    op.drop_column("l5_service_generation_collections", "pending_recommendations")
