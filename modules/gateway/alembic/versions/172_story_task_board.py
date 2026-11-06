"""Retain the live task board on its authenticated run assignment."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "172_story_task_board"
down_revision = "171_l5_linked_draft_owners"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orchestration_run_reports", sa.Column("task_board_snapshot", sa.JSON().with_variant(postgresql.JSONB(), "postgresql")))


def downgrade():
    raise RuntimeError("The delivery release retains task history and cannot safely be reversed")
