"""Install WA attribution exclusions and later-task usage on the gateway lineage."""

revision = "167_l5_wa_results"
down_revision = "166_l5_wa_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_wa_collection_exclusions, l5_wa_usage

    l5_wa_collection_exclusions.upgrade()
    l5_wa_usage.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
