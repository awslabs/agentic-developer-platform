"""Install the additive L5 integration receipt after the existing gateway heads."""

revision = "145_l5_integration_observation"
down_revision = "144_merge_l5_flow_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_devops_integration_observation

    l5_devops_integration_observation.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
