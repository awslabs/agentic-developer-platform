"""Record the specific restore pod so abandoned pins can be recovered safely."""

import sqlalchemy as sa

from alembic import op

revision = "095_crew_restore_pins"
down_revision = "094_crew_checkpoints"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("crew_checkpoints", sa.Column("restore_pod_uid", sa.String(128)))
    op.add_column("crew_checkpoints", sa.Column("restore_pod_name", sa.String(128)))


def downgrade():
    op.drop_column("crew_checkpoints", "restore_pod_name")
    op.drop_column("crew_checkpoints", "restore_pod_uid")
