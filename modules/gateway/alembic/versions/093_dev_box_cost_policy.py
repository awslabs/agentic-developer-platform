"""Require explicit infrastructure price and storage/time limits before admission."""

import sqlalchemy as sa

from alembic import op

revision = "093_dev_box_cost_policy"
down_revision = "092_dev_box_assistant_activity"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("dev_box_policies", sa.Column("max_storage_gb", sa.Integer()))
    op.add_column("dev_box_policies", sa.Column("max_running_hours", sa.Integer()))
    for name in ("instance_hour_rate_usd", "disk_gb_hour_rate_usd", "snapshot_gb_hour_rate_usd"):
        op.add_column("dev_box_policies", sa.Column(name, sa.Numeric(15, 6)))
    op.add_column("dev_box_resources", sa.Column("last_observed_at", sa.DateTime(timezone=True)))
    op.add_column("dev_box_resources", sa.Column("last_observed_state", sa.String(32)))
    op.add_column("dev_box_resources", sa.Column("last_rate_usd", sa.Numeric(15, 6)))
    op.add_column("dev_box_resources", sa.Column("coverage_gap_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("dev_box_resources", "coverage_gap_at")
    op.drop_column("dev_box_resources", "last_rate_usd")
    op.drop_column("dev_box_resources", "last_observed_state")
    op.drop_column("dev_box_resources", "last_observed_at")
    for name in ("snapshot_gb_hour_rate_usd", "disk_gb_hour_rate_usd", "instance_hour_rate_usd"):
        op.drop_column("dev_box_policies", name)
    op.drop_column("dev_box_policies", "max_running_hours")
    op.drop_column("dev_box_policies", "max_storage_gb")
