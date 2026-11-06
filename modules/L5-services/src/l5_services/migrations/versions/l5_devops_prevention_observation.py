"""Keep native prevention readback separate from inbound triggers.

Revision ID: l5_devops_prevention_obs
Revises: l5_devops_native_runs
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "l5_devops_prevention_obs"
down_revision = "l5_devops_native_runs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("l5_devops_space_intents", sa.Column(
        "prevention_observation", sa.JSON().with_variant(postgresql.JSONB(), "postgresql")))
    op.add_column("l5_devops_space_intents", sa.Column(
        "prevention_policy", sa.JSON().with_variant(postgresql.JSONB(), "postgresql")))
    op.add_column("l5_devops_space_intents", sa.Column("prevention_operation_id", sa.String(128)))


def downgrade():
    op.drop_column("l5_devops_space_intents", "prevention_operation_id")
    op.drop_column("l5_devops_space_intents", "prevention_policy")
    op.drop_column("l5_devops_space_intents", "prevention_observation")
