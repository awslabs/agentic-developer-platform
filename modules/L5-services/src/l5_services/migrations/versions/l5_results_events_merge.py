"""Join Security results and event receipt migration histories."""

revision = "l5_results_events_merge"
down_revision = ("l5_results_and_triage", "l5_events_resource_merge")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
