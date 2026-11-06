"""Converge native epic links and Atlassian recovery without dropping either schema."""

revision = "129_merge_native_epic_atlassian"
down_revision = ("128_native_epic_issue_links", "128_atlassian_setup_recovery")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
