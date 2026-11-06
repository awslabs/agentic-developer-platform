"""Join conversation metadata and snapshot timestamps without changing either schema."""

revision = "133_merge_dev_box_conversations"
down_revision = ("132_crew_conversations", "132_dev_box_snapshot_time")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
