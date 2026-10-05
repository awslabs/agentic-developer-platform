"""Join identity revocation and pricing migrations without rewriting either."""

revision = "072_merge_identity_pricing"
down_revision = ("071_installation_revocation", "070_opus55_pricing")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
