"""Join retained L5 and Project budget histories without schema changes."""

revision = "149_merge_l5_project_budgets"
down_revision = ("148_l5_results_knowledge", "146_project_budgets")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
