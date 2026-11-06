"""Join the already-deployed GitLab home and L5 native-run histories."""

revision = "151_merge_gitlab_l5_runs"
down_revision = ("150_gitlab_product_homes", "150_l5_devops_native_runs")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
