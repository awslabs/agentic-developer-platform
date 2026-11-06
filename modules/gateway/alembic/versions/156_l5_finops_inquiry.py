"""Install the durable FinOps inquiry digest on the gateway lineage."""

revision = "156_l5_finops_inquiry"
down_revision = "155_l5_publication_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_finops_inquiry_attempt

    l5_finops_inquiry_attempt.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
