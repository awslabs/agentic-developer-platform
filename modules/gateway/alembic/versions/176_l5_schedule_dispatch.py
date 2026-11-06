"""Install shared L5 scheduler state and retained event observations."""

revision = "176_l5_schedule_dispatch"
down_revision = "175_l5_setup_authority"
branch_labels = None
depends_on = None


def upgrade():
    from l5_services.migrations.versions import l5_schedule_dispatch

    l5_schedule_dispatch.upgrade()


def downgrade():
    raise RuntimeError("The L5 release retains owner authorization and qualification history and cannot safely be reversed")
