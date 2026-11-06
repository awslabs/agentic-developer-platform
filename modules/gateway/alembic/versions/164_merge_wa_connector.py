"""Join WA Profile claims and connector authority without changing retained state."""

revision = "164_merge_wa_connector"
down_revision = ("159_merge_wa_cost", "163_merge_connector_cost")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
