"""Join Security results and binding setup migration histories."""

revision = "l5_results_bindings_merge"
down_revision = ("l5_results_events_merge", "l5_events_bindings_merge")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
