"""Join FinOps ownership and Security publication migration histories."""

revision = "l5_finops_publication_merge"
down_revision = ("l5_finops_setup_receipts", "l5_publication_prevention_merge")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
