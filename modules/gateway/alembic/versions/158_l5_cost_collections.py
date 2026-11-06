"""Install retained FinOps billing collections on the gateway lineage."""

revision = "158_l5_cost_collections"
down_revision = "157_merge_transform_inquiry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_finops_cost_collections

    l5_finops_cost_collections.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
