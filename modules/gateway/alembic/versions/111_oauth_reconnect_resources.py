"""Preserve reconnect authority and independently observed Jira sites.

Revision ID: 111_oauth_reconnect_resources
Revises: 110_oauth_provider_binding
"""

import sqlalchemy as sa

from alembic import op

revision = "111_oauth_reconnect_resources"
down_revision = "110_oauth_provider_binding"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("oauth_connection_attempts", sa.Column("connection_snapshot", sa.JSON(), nullable=True))
    op.add_column("oauth_connection_bindings", sa.Column("observed_resources", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("oauth_connection_bindings", "observed_resources")
    op.drop_column("oauth_connection_attempts", "connection_snapshot")
