"""Converge durable metering and catalog initiatives without changing either schema."""

revision = "120_merge_metering_initiatives"
down_revision = ("119_metering_binding_fence", "116_catalog_initiatives")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
