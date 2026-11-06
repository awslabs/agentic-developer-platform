"""Fence setup leases before issuing scoped worker credentials."""

from alembic import op
import sqlalchemy as sa

revision = "l5_setup_work_fences"
down_revision = "l5_cost_transform_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("l5_service_setup_work", sa.Column("lease_owner", sa.String(255)))
    op.add_column("l5_service_setup_work", sa.Column("fence", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("l5_service_setup_work", "fence")
    op.drop_column("l5_service_setup_work", "lease_owner")
