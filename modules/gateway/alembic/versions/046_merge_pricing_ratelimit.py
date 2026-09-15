"""Join the independently delivered pricing and org rate-limit migrations.

Both branches descend from043. Keeping their revision IDs intact lets existing
installations at either head converge using the normal alembic upgrade head.
"""

revision = "046_merge_pricing_ratelimit"
down_revision = ("045_pricing_seed_2026_09_12_1", "044_ratelimit_org_type")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
