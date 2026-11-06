"""Join retained WA Profile claims and FinOps billing checkpoint histories."""

revision = "l5_wa_cost_merge"
down_revision = ("l5_wa_transform_merge", "l5_cost_transform_merge")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
