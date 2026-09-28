"""Join the budget journal and durable CLI lifecycle migration branches."""

revision = "022_merge_budget_cli"
down_revision = ("018_add_operation_budget_reservations", "021_deployment_identity")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
