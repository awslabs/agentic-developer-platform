"""Join event receipts and DevOps space intents without rewriting either history."""

revision = "l5_events_devops_merge"
down_revision = ("l5_events", "l5_devops_space_intents")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
