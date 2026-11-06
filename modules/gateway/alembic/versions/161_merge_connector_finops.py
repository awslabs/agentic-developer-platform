"""Join connector authority and FinOps inquiry histories without changing state."""

revision = "161_merge_connector_finops"
down_revision = ("160_connector_cell_replay", "156_l5_finops_inquiry")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
