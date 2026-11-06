"""Join L5 recovery receipts and retained request analytics without schema changes."""

revision = "146_merge_l5_request_logs"
down_revision = ("145_l5_integration_observation", "144_request_logs")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
