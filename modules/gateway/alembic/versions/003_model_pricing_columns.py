"""Rename model_pricing columns and add source field.

Issue #234: Budget Usage Tracking Lambda

Revision ID: 003_model_pricing
Revises: 3e525b1eg90e
Create Date: 2026-02-27
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "003_model_pricing"
down_revision = "3e525b1eg90e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("model_pricing", "input_price_per_1k", new_column_name="input_price_per_1k_tokens")
    op.alter_column("model_pricing", "output_price_per_1k", new_column_name="output_price_per_1k_tokens")
    op.add_column("model_pricing", sa.Column("source", sa.String(20), nullable=False, server_default="pricing_api"))


def downgrade() -> None:
    op.drop_column("model_pricing", "source")
    op.alter_column("model_pricing", "output_price_per_1k_tokens", new_column_name="output_price_per_1k")
    op.alter_column("model_pricing", "input_price_per_1k_tokens", new_column_name="input_price_per_1k")
