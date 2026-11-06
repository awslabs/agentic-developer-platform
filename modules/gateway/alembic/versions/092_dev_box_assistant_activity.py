"""Track assistant activity independently of host activity for idle enforcement."""

import sqlalchemy as sa

from alembic import op

revision = "092_dev_box_assistant_activity"
down_revision = "091_merge_dev_box_crew_usage"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("dev_boxes", sa.Column("assistant_activity_at", sa.DateTime(timezone=True)))
    op.add_column("dev_boxes", sa.Column("assistant_busy_until_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("dev_boxes", "assistant_busy_until_at")
    op.drop_column("dev_boxes", "assistant_activity_at")
