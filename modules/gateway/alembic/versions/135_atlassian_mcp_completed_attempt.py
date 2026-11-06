"""Record Identity completion before admitting separate adapter observation."""

import sqlalchemy as sa

from alembic import op

revision = "135_mcp_identity_completion"
down_revision = "134_atlassian_mcp_selected_site"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("oauth_connection_attempts", sa.Column("identity_completed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("oauth_connection_attempts", "identity_completed_at")
