"""Join Well-Architected Profile claims and publication retry histories."""

revision = "l5_wa_retry_merge"
down_revision = ("l5_finops_wa_merge", "l5_finops_retry_merge")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
