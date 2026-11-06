"""Retain the scopes requested for owner-bound OAuth consent attempts."""

import sqlalchemy as sa

from alembic import op

revision = "109_oauth_scope_snapshot"
down_revision = "108_native_credentials"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("oauth_connection_attempts", sa.Column("requested_scopes", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("oauth_connection_attempts", "requested_scopes")
