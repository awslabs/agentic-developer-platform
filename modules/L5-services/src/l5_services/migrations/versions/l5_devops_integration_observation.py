"""Store scoped DevOps integration association recovery receipts.

Revision ID: l5_devops_integration_obs
Revises: l5_devops_resource_observation
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "l5_devops_integration_obs"
down_revision = "l5_devops_resource_observation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("l5_devops_space_intents", sa.Column(
        "integration_observation", sa.JSON().with_variant(postgresql.JSONB(), "postgresql")))


def downgrade():
    op.drop_column("l5_devops_space_intents", "integration_observation")
