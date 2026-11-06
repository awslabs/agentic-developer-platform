"""Install retained native Profile claims on the gateway migration lineage."""

revision = "155_l5_wa_profile_claims"
down_revision = "154_l5_finops_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_wa_profile_claims

    l5_wa_profile_claims.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
