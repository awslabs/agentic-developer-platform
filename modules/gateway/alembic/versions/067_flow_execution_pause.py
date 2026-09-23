"""Start existing and new flows paused until an operator resumes them."""

import sqlalchemy as sa

from alembic import op

revision = "067_flow_execution_pause"
down_revision = "066_cred_evidence_delegation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orchestration_flows", sa.Column("execution_paused", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("orchestration_flows", "execution_paused")
