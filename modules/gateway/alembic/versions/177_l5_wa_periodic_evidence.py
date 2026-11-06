"""Install retained WA periodic checkpoints and mandatory transport observations."""

revision = "177_l5_wa_periodic_evidence"
down_revision = "176_l5_schedule_dispatch"
branch_labels = None
depends_on = None


def upgrade():
    from l5_services.migrations.versions import l5_wa_periodic_evidence

    l5_wa_periodic_evidence.upgrade()


def downgrade():
    raise RuntimeError("The L5 release retains owner authorization and qualification history and cannot safely be reversed")
