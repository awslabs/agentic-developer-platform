"""Install native DevOps identity and monitoring on the gateway lineage."""

revision = "150_l5_devops_native_runs"
down_revision = "149_merge_l5_project_budgets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_devops_native_runs

    l5_devops_native_runs.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
