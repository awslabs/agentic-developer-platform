"""Join Security delegation and WA history and install fenced setup leases."""

revision = "168_merge_security_wa"
down_revision = ("164_l5_work_delegation_parent", "167_l5_wa_results")
branch_labels = None
depends_on = None


def upgrade():
    from l5_services.migrations.versions import l5_setup_work_fences

    l5_setup_work_fences.upgrade()


def downgrade():
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
