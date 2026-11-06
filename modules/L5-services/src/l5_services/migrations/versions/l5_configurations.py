"""Add L5 Security configurations and immutable versions.

Revision ID: l5_configurations
Revises:
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "l5_configurations"
down_revision = None
branch_labels = ("l5",)
depends_on = None


def upgrade():
    op.create_table(
        "l5_service_configurations",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("configuration_id", sa.String(36), primary_key=True),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("service_id", sa.String(255), nullable=False),
        sa.Column("relationship_revision", sa.String(255), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("handoff_state", sa.String(16), nullable=False),
        sa.UniqueConstraint("tenant_id", "product_id", "project_id", "service_id"),
    )
    op.create_table(
        "l5_service_config_versions",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("configuration_id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("settings", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("inherited_fields", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("gaps", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "configuration_id"],
            ["l5_service_configurations.tenant_id", "l5_service_configurations.configuration_id"],
        ),
    )
    op.create_table(
        "l5_service_config_receipts",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("product_id", sa.String(255), primary_key=True),
        sa.Column("project_id", sa.String(255), primary_key=True),
        sa.Column("service_id", sa.String(255), primary_key=True),
        sa.Column("principal_id", sa.String(255), primary_key=True),
        sa.Column("operation", sa.String(32), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("configuration_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("l5_service_config_receipts")
    op.drop_table("l5_service_config_versions")
    op.drop_table("l5_service_configurations")
