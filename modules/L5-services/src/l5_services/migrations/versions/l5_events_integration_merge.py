"""Join event receipts and DevOps integration observation migration histories."""

revision = "l5_events_integration_merge"
down_revision = ("l5_events_resource_merge", "l5_devops_integration_obs")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
