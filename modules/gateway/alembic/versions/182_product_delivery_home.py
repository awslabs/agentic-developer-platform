"""Add an optional delivery home repository to Products."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "182_product_delivery_home"
down_revision = "181_atlassian_forge"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("delivery_home_repository", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True))


def downgrade():
    op.drop_column("products", "delivery_home_repository")
