"""Install retained source holds on the gateway migration lineage."""

revision = "165_l5_source_retention"
down_revision = "164_merge_wa_connector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_source_retention

    l5_source_retention.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
