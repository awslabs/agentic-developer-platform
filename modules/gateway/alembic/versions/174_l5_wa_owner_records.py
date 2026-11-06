"""Install retained WA owner records on the Gateway database lineage."""

revision = "174_l5_wa_owner_records"
down_revision = "173_l5_execution_scope"
branch_labels = None
depends_on = None


def upgrade():
    from l5_services.migrations.versions import l5_wa_qualification, l5_wa_reuse_authorization

    l5_wa_reuse_authorization.upgrade()
    l5_wa_qualification.upgrade()


def downgrade():
    raise RuntimeError("The L5 release retains owner authorization and qualification history and cannot safely be reversed")
