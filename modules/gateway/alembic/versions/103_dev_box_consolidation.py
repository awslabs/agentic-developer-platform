"""Record explicit assistant consolidation separately from activity."""

import sqlalchemy as sa

from alembic import op

revision = "103_dev_box_consolidation"
down_revision = "102_crew_owner_generation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("dev_boxes", sa.Column("assistant_consolidated_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("dev_boxes", "assistant_consolidated_at")
