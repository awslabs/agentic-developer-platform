"""Join durable metering and Product scope guard migrations."""

revision = "114_merge_metering_product_scope"
down_revision = ("113_metering_authority", "113_product_scope_guard")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
