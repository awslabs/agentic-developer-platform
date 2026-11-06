"""Add nullable flow planning reference projections.

Revision ID: 125_flow_planning_references
Revises: 124_flow_product_repo
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "125_flow_planning_references"
down_revision = "124_flow_product_repo"
branch_labels = None
depends_on = None


def upgrade():
    document = sa.JSON().with_variant(JSONB(), "postgresql")
    op.add_column("orchestration_flows", sa.Column("origin_ref", document, nullable=True))
    op.add_column("orchestration_flows", sa.Column("tracker_refs", document, nullable=True))


def downgrade():
    op.drop_column("orchestration_flows", "tracker_refs")
    op.drop_column("orchestration_flows", "origin_ref")
