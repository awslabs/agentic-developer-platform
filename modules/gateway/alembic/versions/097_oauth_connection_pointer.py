"""Store Identity pointer provenance and short-lived OAuth consent attempts."""

import sqlalchemy as sa

from alembic import op

revision = "097_oauth_connection_pointer"
down_revision = "096_flow_archive"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_credentials", sa.Column("provider", sa.String(32), nullable=True))
    op.create_table(
        "oauth_connection_attempts",
        sa.Column("state_digest", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("session_uri", sa.String(2048), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_oauth_connection_attempts_org_id", "oauth_connection_attempts", ["org_id"])


def downgrade():
    op.drop_index("ix_oauth_connection_attempts_org_id", table_name="oauth_connection_attempts")
    op.drop_table("oauth_connection_attempts")
    op.drop_column("user_credentials", "provider")
