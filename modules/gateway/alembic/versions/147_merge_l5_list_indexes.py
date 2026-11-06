"""Join retained L5 history and conversation list indexes without schema changes."""

revision = "147_merge_l5_list_indexes"
down_revision = ("146_merge_l5_request_logs", "145_conversation_list_indexes")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
