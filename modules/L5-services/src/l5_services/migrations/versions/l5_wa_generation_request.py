"""Store the pinned native recommendation request before Start.

Revision ID: l5_wa_generation_request
Revises: l5_source_retention
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from l5_services.migrations.retained_downgrade import require_empty

revision = "l5_wa_generation_request"
down_revision = "l5_source_retention"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("l5_service_worker_attempts", sa.Column(
        "prepared_request", sa.JSON().with_variant(JSONB(), "postgresql")))


def downgrade():
    require_empty("l5_service_worker_attempts", "prepared_request")
    op.drop_column("l5_service_worker_attempts", "prepared_request")
