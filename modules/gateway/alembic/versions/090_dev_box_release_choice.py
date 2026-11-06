"""Persist the destructive Dev Box release choice with its admitted operation."""

import sqlalchemy as sa

from alembic import op

revision = "090_dev_box_release_choice"
down_revision = "089_merge_dev_box_crew"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("dev_box_operations", sa.Column("retention_choice", sa.String(16), nullable=True))


def downgrade():
    op.drop_column("dev_box_operations", "retention_choice")
