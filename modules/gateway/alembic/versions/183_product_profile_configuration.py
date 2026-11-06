"""Shared Product repository purposes and named environments."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "183_product_profile_config"
down_revision = "182_product_delivery_home"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("profile_configuration", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True))


def downgrade():
    op.drop_column("products", "profile_configuration")
