"""Join FinOps ownership and publication retry receipt migration histories."""

revision = "l5_finops_retry_merge"
down_revision = ("l5_finops_publication_merge", "l5_publication_retry_receipts")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
