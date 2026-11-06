"""Record the selected Atlassian MCP site without altering legacy Jira grants."""

import sqlalchemy as sa

from alembic import op

revision = "134_atlassian_mcp_selected_site"
down_revision = "133_merge_dev_box_conversations"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("oauth_connection_bindings", sa.Column("selected_site_id", sa.String(512), nullable=True))


def downgrade():
    op.drop_column("oauth_connection_bindings", "selected_site_id")
