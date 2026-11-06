"""Persist a fail-closed adapter release fence before admitting tool calls."""

import sqlalchemy as sa

from alembic import op

revision = "136_tool_adapter_activation"
down_revision = "135_mcp_identity_completion"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tool_adapter_activations",
        sa.Column("group", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("release", sa.JSON(), nullable=False),
        sa.CheckConstraint("status IN ('legacy','fenced','active')", name="ck_tool_adapter_activation_status"),
    )

    op.add_column("oauth_connection_attempts", sa.Column("identity_completion_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("oauth_connection_attempts", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("oauth_tool_receipts", sa.Column("authorization", sa.JSON(), nullable=True))
    op.add_column("oauth_tool_receipts", sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "oauth_grant_revocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("owner_ref", sa.String(255), nullable=False),
        sa.Column("grant_revision", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("connection_id", sa.String(36)),
        sa.Column("provider_account_id", sa.String(255)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error_category", sa.String(64)),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "owner_ref", "grant_revision", name="uq_oauth_grant_revocation_generation"),
    )
    op.create_index("ix_oauth_grant_revocation_due", "oauth_grant_revocations", ["state", "due_at"])


def downgrade():
    raise RuntimeError("Retain adapter activation and group generation during rollback")
