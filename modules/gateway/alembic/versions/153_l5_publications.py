"""Install durable Security Context publication on the gateway lineage."""

revision = "153_l5_publications"
down_revision = "152_merge_gitlab_prevention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_publications

    l5_publications.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
