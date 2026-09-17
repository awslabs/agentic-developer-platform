"""Add cognito_sub column to organizations table.

Revision ID: 006
Revises: 005
Create Date: 2026-04-09
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "006"
down_revision = "005"
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
