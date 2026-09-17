"""Add org settings and SSO configuration columns.

Revision ID: 007
Revises: 006
Create Date: 2026-04-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Org settings columns
    op.add_column(
        "organizations",
        sa.Column("billing_email", sa.String(320), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("allowed_clouds", JSONB, nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("default_quotas", JSONB, nullable=True),
    )

    # SSO configuration columns
    op.add_column(
        "organizations",
        sa.Column("sso_provider", sa.String(50), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("sso_metadata_url", sa.String(2048), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("sso_provider_name", sa.String(255), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("sso_provider_type", sa.String(10), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("sso_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("organizations", "sso_enabled")
    op.drop_column("organizations", "sso_provider_type")
    op.drop_column("organizations", "sso_provider_name")
    op.drop_column("organizations", "sso_metadata_url")
    op.drop_column("organizations", "sso_provider")
    op.drop_column("organizations", "default_quotas")
    op.drop_column("organizations", "allowed_clouds")
    op.drop_column("organizations", "billing_email")
