"""Join connector authority and billing collections without changing retained state."""

revision = "163_merge_connector_cost"
down_revision = ("162_merge_connector_transform", "158_l5_cost_collections")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
