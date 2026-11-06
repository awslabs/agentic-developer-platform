"""Join FinOps ownership and Transform cleanup migration histories."""

revision = "l5_finops_transform_merge"
down_revision = ("l5_finops_publication_merge", "l5_transform_cleanup")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
