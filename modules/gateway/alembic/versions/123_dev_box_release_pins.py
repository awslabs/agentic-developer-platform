"""Dev Box instances record the release their generation was provisioned from (#8223 cause 34).

A stop/start of the same generation used to be verified against whatever release was current
when the box came back; any bundle rebuild in between stranded every running box at its next
restart. The controller now records the pins at launch and verifies against them.

Revision ID: 123_dev_box_release_pins
Revises: 122_product_handoff_callbacks
"""

import sqlalchemy as sa
from alembic import op

revision = "123_dev_box_release_pins"
down_revision = "122_product_handoff_callbacks"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("dev_box_instances", sa.Column("profile_revision", sa.String(64)))
    op.add_column("dev_box_instances", sa.Column("source_revision", sa.String(64)))
    op.add_column("dev_box_instances", sa.Column("crew_bundle_sha256", sa.String(64)))
    op.add_column("dev_box_instances", sa.Column("launch_template_version", sa.String(32)))


def downgrade():
    op.drop_column("dev_box_instances", "launch_template_version")
    op.drop_column("dev_box_instances", "crew_bundle_sha256")
    op.drop_column("dev_box_instances", "source_revision")
    op.drop_column("dev_box_instances", "profile_revision")
