"""Hold only guest public keys and encrypted, short-lived host challenges."""

import sqlalchemy as sa

from alembic import op

revision = "099_dev_box_sealed_enrollment"
down_revision = "098_tool_entitlements"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("dev_box_instances", sa.Column("enrollment_public_key", sa.LargeBinary()))
    op.add_column("dev_box_devices", sa.Column("sealed_enrollment", sa.LargeBinary()))


def downgrade():
    op.drop_column("dev_box_devices", "sealed_enrollment")
    op.drop_column("dev_box_instances", "enrollment_public_key")
