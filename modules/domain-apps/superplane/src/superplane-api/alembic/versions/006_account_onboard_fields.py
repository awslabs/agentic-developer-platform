"""Add external_id, irsa_role_arns_json, secret_arns_json to cloud_accounts for BYOA onboarding.

Revision ID: 006_account_onboard_fields
Revises: 005_add_research_proposals
Create Date: 2026-04-09
"""

from alembic import op
import sqlalchemy as sa

revision = "006_account_onboard_fields"
down_revision = "005_add_research_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cloud_accounts", sa.Column("external_id", sa.String(255), nullable=True)
    )
    op.add_column(
        "cloud_accounts", sa.Column("irsa_role_arns_json", sa.Text, nullable=True)
    )
    op.add_column(
        "cloud_accounts", sa.Column("secret_arns_json", sa.Text, nullable=True)
    )
    op.add_column(
        "cloud_accounts", sa.Column("ingest_role_arn", sa.String(512), nullable=True)
    )

    # Add unique constraint on (org_id, account_identifier) to prevent duplicate onboarding
    op.create_unique_constraint(
        "uq_cloud_accounts_org_account",
        "cloud_accounts",
        ["org_id", "account_identifier"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_cloud_accounts_org_account", "cloud_accounts", type_="unique"
    )
    op.drop_column("cloud_accounts", "ingest_role_arn")
    op.drop_column("cloud_accounts", "secret_arns_json")
    op.drop_column("cloud_accounts", "irsa_role_arns_json")
    op.drop_column("cloud_accounts", "external_id")
