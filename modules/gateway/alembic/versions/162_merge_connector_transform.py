"""Join connector authority and Transform cleanup without changing retained state."""

revision = "162_merge_connector_transform"
down_revision = ("161_merge_connector_finops", "157_merge_transform_inquiry")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
