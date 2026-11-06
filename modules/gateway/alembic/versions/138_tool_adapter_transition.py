"""Retain adapter handoff progress and the original rollback manifest."""

import sqlalchemy as sa

from alembic import op

revision = "138_tool_adapter_transition"
down_revision = "137_platform_admin_grants"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tool_adapter_activations", sa.Column("transition", sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError("Retain the adapter transition and its rollback manifest")
