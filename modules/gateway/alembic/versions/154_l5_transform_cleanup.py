"""Install additive Transform binding cleanup receipt on gateway lineage."""

revision = "154_l5_transform_cleanup"
down_revision = "153_l5_publications"
branch_labels = None
depends_on = None


def upgrade():
    from l5_services.migrations.versions import l5_transform_cleanup

    l5_transform_cleanup.upgrade()


def downgrade():
    raise RuntimeError("Retained cleanup evidence cannot safely be removed")
