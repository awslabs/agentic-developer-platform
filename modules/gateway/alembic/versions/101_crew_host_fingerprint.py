"""Pin the current EC2 Crew host certificate to its machine generation."""

import sqlalchemy as sa

from alembic import op

revision = "101_crew_host_fingerprint"
down_revision = "100_crew_host_restore_pin"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("dev_box_instances", sa.Column("certificate_fingerprint", sa.String(64)))


def downgrade():
    op.drop_column("dev_box_instances", "certificate_fingerprint")
