"""Join retained WA Profile claims and FinOps billing checkpoint histories."""

revision = "159_merge_wa_cost"
down_revision = ("158_merge_wa_transform", "158_l5_cost_collections")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
