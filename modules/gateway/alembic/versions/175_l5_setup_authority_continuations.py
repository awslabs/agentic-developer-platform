"""Install append-only setup authority on the Gateway migration lineage."""

revision = "175_l5_setup_authority"
down_revision = "174_l5_wa_owner_records"
branch_labels = None
depends_on = None


def upgrade():
    from l5_services.migrations.versions import l5_setup_authority_continuations

    l5_setup_authority_continuations.upgrade()


def downgrade():
    raise RuntimeError("The L5 release retains owner authorization and qualification history and cannot safely be reversed")
