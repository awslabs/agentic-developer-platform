"""Join Transform cleanup and publication retry receipts on the gateway lineage."""

revision = "156_merge_transform_retry"
down_revision = ("155_merge_finops_transform", "155_l5_publication_retry")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
