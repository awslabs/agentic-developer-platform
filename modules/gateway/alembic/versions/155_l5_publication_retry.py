"""Install durable publication retry receipts on the gateway lineage."""

revision = "155_l5_publication_retry"
down_revision = "154_l5_finops_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_publication_retry_receipts

    l5_publication_retry_receipts.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
