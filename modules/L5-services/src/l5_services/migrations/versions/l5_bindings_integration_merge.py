"""Join Security binding and DevOps integration migration histories."""

revision = "l5_bindings_integration_merge"
down_revision = ("l5_events_bindings_merge", "l5_events_integration_merge")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
