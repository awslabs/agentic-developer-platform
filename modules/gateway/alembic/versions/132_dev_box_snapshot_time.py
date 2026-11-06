"""Preserve provider snapshot creation time independently of observation ticks."""

import sqlalchemy as sa

from alembic import op

revision = "132_dev_box_snapshot_time"
down_revision = "131_legacy_binding_migration"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("dev_box_resources", sa.Column("snapshot_created_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("dev_box_resources", "snapshot_created_at")
