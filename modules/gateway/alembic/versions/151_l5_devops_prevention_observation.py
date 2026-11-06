"""Persist DevOps native prevention readback on the gateway lineage."""

revision = "151_l5_devops_prevention_obs"
down_revision = "150_l5_devops_native_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_devops_prevention_observation

    l5_devops_prevention_observation.upgrade()


def downgrade() -> None:
    from l5_services.migrations.versions import l5_devops_prevention_observation

    l5_devops_prevention_observation.downgrade()
