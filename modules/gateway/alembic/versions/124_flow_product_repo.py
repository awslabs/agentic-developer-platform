"""Add nullable Product and story repository projections.

Revision ID: 124_flow_product_repo
Revises: 123_dev_box_release_pins
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "124_flow_product_repo"
down_revision = "123_dev_box_release_pins"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orchestration_flows", sa.Column("product_id", sa.String(36), nullable=True))
    op.create_index("ix_orchestration_flows_product_id", "orchestration_flows", ["product_id"])
    op.add_column("orchestration_nodes", sa.Column("repository_identity", sa.String(255), nullable=True))
    op.add_column("orchestration_nodes", sa.Column("provider_repository_id", sa.BigInteger(), nullable=True))
    op.add_column("orchestration_nodes", sa.Column("repository_connection", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True))
    op.create_index(
        "ix_orchestration_nodes_org_repository_issue",
        "orchestration_nodes",
        ["org_id", "provider_repository_id", "issue_ref"],
    )


def downgrade():
    op.drop_index("ix_orchestration_nodes_org_repository_issue", table_name="orchestration_nodes")
    op.drop_column("orchestration_nodes", "repository_connection")
    op.drop_column("orchestration_nodes", "provider_repository_id")
    op.drop_column("orchestration_nodes", "repository_identity")
    op.drop_index("ix_orchestration_flows_product_id", table_name="orchestration_flows")
    op.drop_column("orchestration_flows", "product_id")
