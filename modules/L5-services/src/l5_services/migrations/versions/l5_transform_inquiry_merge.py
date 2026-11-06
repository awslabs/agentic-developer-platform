"""Join Transform cleanup and FinOps inquiry histories without rewriting either."""

revision = "l5_transform_inquiry_merge"
down_revision = ("l5_transform_retry_merge", "l5_finops_inquiry_retry_merge")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
