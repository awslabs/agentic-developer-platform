"""Create cli_auth_requests table for web-based CLI login.

Device-authorization-style flow: `bg-cognito-auth.sh login --web` creates a
pending row, the signed-in browser user approves it on /cli-auth, and the CLI
redeems it (single-use) to receive tokens minted on the CLI app client.

Revision ID: 038_cli_auth_requests
Revises: 037_bedrock_account_routing
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "038_cli_auth_requests"
down_revision: str | None = "037_bedrock_account_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cli_auth_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_code", sa.String(16), nullable=False),
        sa.Column("device_code_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("approved_username", sa.String(255), nullable=True),
        sa.Column("approved_sub", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_cli_auth_requests_user_code", "cli_auth_requests", ["user_code"])
    op.create_index(
        "ux_cli_auth_requests_device_code_hash",
        "cli_auth_requests",
        ["device_code_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("cli_auth_requests")
