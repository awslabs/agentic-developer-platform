"""Add cognito_sub column to organizations table.

Revision ID: 006_add_cognito_sub
Revises: 006_add_event_audit_columns
Create Date: 2026-04-09
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "006_add_cognito_sub"
down_revision = "006_add_event_audit_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("cognito_sub", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_organizations_cognito_sub",
        "organizations",
        ["cognito_sub"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_organizations_cognito_sub", table_name="organizations")
    op.drop_column("organizations", "cognito_sub")
