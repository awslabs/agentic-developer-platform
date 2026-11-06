"""Persist optional node lineage and bounded labels on the graph floor."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "086_orchestration_node_metadata"
down_revision = "086_operations_report_receipt"
branch_labels = None
depends_on = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("orchestration_nodes", sa.Column("lineage", JSON_DOC, nullable=True))
    op.add_column("orchestration_nodes", sa.Column("labels", JSON_DOC, nullable=True))


def downgrade() -> None:
    op.drop_column("orchestration_nodes", "labels")
    op.drop_column("orchestration_nodes", "lineage")
