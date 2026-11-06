"""Record verified Crew run owners on raw usage rows."""

import sqlalchemy as sa

from alembic import op

revision = "090_crew_usage_root"
down_revision = "089_merge_dev_box_crew"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_logs", sa.Column("root_human_id", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_logs", "root_human_id")
