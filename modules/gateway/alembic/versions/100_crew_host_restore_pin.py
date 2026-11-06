"""Keep verified Crew restore pins scoped to the exact owned machine generation."""

import sqlalchemy as sa

from alembic import op

revision = "100_crew_host_restore_pin"
down_revision = "099_dev_box_sealed_enrollment"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("crew_checkpoints", sa.Column("restore_box_id", sa.String(255)))
    op.add_column("crew_checkpoints", sa.Column("restore_machine_generation", sa.Integer()))


def downgrade():
    op.drop_column("crew_checkpoints", "restore_machine_generation")
    op.drop_column("crew_checkpoints", "restore_box_id")
