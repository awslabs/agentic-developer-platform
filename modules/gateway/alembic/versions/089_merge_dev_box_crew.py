"""Join Dev Box admission and Crew inventory without rewriting either history."""

revision = "089_merge_dev_box_crew"
down_revision = ("088_dev_box_contract", "088_crew_executor_inventory")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
