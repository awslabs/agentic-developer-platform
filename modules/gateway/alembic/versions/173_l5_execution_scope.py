"""Retain exact owner consent independently of audit history."""

import sqlalchemy as sa

from alembic import op

revision = "173_l5_execution_scope"
down_revision = "172_story_task_board"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("credential_service_delegations", sa.Column("execution_scope", sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError("This migration retains execution authority and cannot safely be reversed")
