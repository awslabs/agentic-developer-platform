"""Persist DevOps knowledge synchronization intents and asset observations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "l5_devops_knowledge_pub"
down_revision = "l5_bindings_integration_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "l5_devops_knowledge_publications",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("space_id", sa.String(255), primary_key=True),
        sa.Column("source_key", sa.String(64), primary_key=True),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("operation_id", sa.String(255), nullable=False),
        sa.Column("intent", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("asset_id", sa.String(255)),
        sa.Column("asset_version", sa.Integer()),
        sa.Column("error", sa.String(255)),
    )


def downgrade():
    op.drop_table("l5_devops_knowledge_publications")
