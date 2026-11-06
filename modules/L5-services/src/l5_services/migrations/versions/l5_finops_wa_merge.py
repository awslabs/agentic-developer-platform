"""Join FinOps ownership and Well-Architected Profile claim histories."""

revision = "l5_finops_wa_merge"
down_revision = ("l5_finops_publication_merge", "l5_wa_profile_claims")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
