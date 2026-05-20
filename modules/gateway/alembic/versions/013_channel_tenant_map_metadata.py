"""Add metadata JSON column to channel_tenant_map.

Revision ID: 013_channel_tenant_map_metadata
Revises: 012_unique_cognito_sub
Create Date: 2026-05-20
"""

import sqlalchemy as sa
from alembic import op

revision = "013_channel_tenant_map_metadata"
down_revision = "012_unique_cognito_sub"


def upgrade() -> None:
    op.add_column(
        "channel_tenant_map",
        sa.Column("metadata", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_tenant_map", "metadata")
