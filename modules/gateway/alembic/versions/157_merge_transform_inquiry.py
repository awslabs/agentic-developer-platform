"""Join Transform cleanup and FinOps inquiry histories without rewriting either."""

revision = "157_merge_transform_inquiry"
down_revision = ("156_merge_transform_retry", "156_l5_finops_inquiry")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
