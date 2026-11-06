"""Join Transform cleanup and publication retry receipt histories."""

revision = "l5_transform_retry_merge"
down_revision = ("l5_finops_transform_merge", "l5_finops_retry_merge")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
