"""Join Well-Architected Profile claims and FinOps inquiry histories."""

revision = "l5_wa_finops_inquiry_merge"
down_revision = ("l5_wa_retry_merge", "l5_finops_inquiry_retry_merge")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
