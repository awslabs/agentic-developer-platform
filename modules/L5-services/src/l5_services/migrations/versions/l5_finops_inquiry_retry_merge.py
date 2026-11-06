"""Join durable FinOps inquiry and publication retry migration histories."""

revision = "l5_finops_inquiry_retry_merge"
down_revision = ("l5_finops_inquiry_attempt", "l5_finops_retry_merge")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
