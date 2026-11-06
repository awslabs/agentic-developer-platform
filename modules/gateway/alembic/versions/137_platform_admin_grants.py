"""Persist global admin grants and revocations independently of token claims."""

import sqlalchemy as sa

from alembic import op

revision = "137_platform_admin_grants"
down_revision = "136_tool_adapter_activation"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("platform_admin_grants"):
        return
    op.create_table(
        "platform_admin_grants",
        sa.Column("cognito_sub", sa.String(255), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    # Revocations are security state. An older application may ignore the table,
    # but a rollback followed by an upgrade must not resurrect revoked admins.
    pass
