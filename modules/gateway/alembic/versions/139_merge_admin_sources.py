"""Join admin revocations and Product source receipts without changing either schema."""

revision = "139_merge_admin_sources"
down_revision = ("137_platform_admin_grants", "138_product_source_snapshots")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
