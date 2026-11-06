"""Persist tenant tool grants and team overrides, denying absent tenant grants."""

import sqlalchemy as sa

from alembic import op

revision = "098_tool_entitlements"
down_revision = "097_oauth_connection_pointer"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tool_entitlements",
        sa.Column("org_id", sa.String(255), primary_key=True),
        sa.Column("team_id", sa.String(255), primary_key=True),
        sa.Column("tool_name", sa.String(128), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("max_cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("max_cost_usd IS NULL OR max_cost_usd > 0", name="ck_tool_entitlement_positive_cost"),
    )


def downgrade():
    op.drop_table("tool_entitlements")
