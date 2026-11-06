"""Store an explicit agent consent decision on each credential."""

import sqlalchemy as sa

from alembic import op

revision = "094_credential_agent_consent"
down_revision = "093_dev_box_cost_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_credentials", sa.Column("agent_consent", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    op.drop_column("user_credentials", "agent_consent")
