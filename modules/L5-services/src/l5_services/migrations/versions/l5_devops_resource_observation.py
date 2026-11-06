"""Store scoped readback for DevOps resource verification.

Revision ID: l5_devops_resource_observation
Revises: l5_devops_space_intents
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "l5_devops_resource_observation"
down_revision = "l5_devops_space_intents"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("l5_devops_space_intents", sa.Column(
        "resource_observation", sa.JSON().with_variant(postgresql.JSONB(), "postgresql")))


def downgrade():
    op.drop_column("l5_devops_space_intents", "resource_observation")
