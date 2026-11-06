"""Join retained adapter transitions and Product source receipts without schema changes."""

revision = "140_merge_adapter_sources"
down_revision = ("139_merge_admin_sources", "138_tool_adapter_transition")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
