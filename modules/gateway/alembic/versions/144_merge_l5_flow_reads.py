"""Join retained L5 history and flow read invalidation without schema changes."""

revision = "144_merge_l5_flow_reads"
down_revision = ("143_merge_l5_conversations", "143_flow_read_revisions")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
