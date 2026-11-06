"""Keep provider-observed identity and one-use receipts separate from consent."""

import sqlalchemy as sa

from alembic import op

revision = "110_oauth_provider_binding"
down_revision = "109_oauth_scope_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "oauth_connection_bindings",
        sa.Column("credential_id", sa.String(36), sa.ForeignKey("user_credentials.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("revision", sa.String(36), nullable=False),
        sa.Column("credential_digest", sa.String(64), nullable=False),
        sa.Column("provider_account_id", sa.String(255), nullable=False),
        sa.Column("observed_scopes", sa.JSON(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_oauth_connection_bindings_org_id", "oauth_connection_bindings", ["org_id"])
    op.create_table(
        "oauth_tool_receipts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_oauth_tool_receipts_expires_at", "oauth_tool_receipts", ["expires_at"])


def downgrade():
    op.drop_index("ix_oauth_tool_receipts_expires_at", table_name="oauth_tool_receipts")
    op.drop_table("oauth_tool_receipts")
    op.drop_index("ix_oauth_connection_bindings_org_id", table_name="oauth_connection_bindings")
    op.drop_table("oauth_connection_bindings")
