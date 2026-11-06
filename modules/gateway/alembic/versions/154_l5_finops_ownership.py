"""Install retained FinOps scope claims and setup receipts on the gateway lineage."""

revision = "154_l5_finops_ownership"
down_revision = "153_l5_publications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_finops_retained_scope, l5_finops_setup_receipts

    l5_finops_retained_scope.upgrade()
    l5_finops_setup_receipts.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
