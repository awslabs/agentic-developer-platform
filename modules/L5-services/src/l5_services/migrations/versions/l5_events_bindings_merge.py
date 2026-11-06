"""Join event recovery and Security binding migration histories."""

revision = "l5_events_bindings_merge"
down_revision = ("l5_events_resource_merge", "l5_bindings_and_setup")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
