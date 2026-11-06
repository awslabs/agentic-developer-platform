"""Join GitLab home and native prevention histories without changing saved state."""

revision = "152_merge_gitlab_prevention"
down_revision = ("151_merge_gitlab_l5_runs", "151_l5_devops_prevention_obs")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
