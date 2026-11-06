"""Join event receipts and DevOps resource observation migration histories."""

revision = "l5_events_resource_merge"
down_revision = ("l5_events_devops_merge", "l5_devops_resource_observation")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
