"""Persist DevOps creation receipts before native effects.

Revision ID: l5_devops_space_intents
Revises: l5_worker_fences
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "l5_devops_space_intents"
down_revision = "l5_worker_fences"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "l5_devops_space_intents",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("selection", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), nullable=False),
        sa.Column("intent_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("native_space_id", sa.String(255)),
        sa.Column("ownership", sa.String(32)),
        sa.Column("scope_fingerprint", sa.String(64)),
        sa.Column("owned_trigger_ids", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("resume_trigger_ids", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("active_executions", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("update_operation_id", sa.String(36)),
        sa.Column("update_configuration_revision", sa.Integer()),
        sa.Column("requested_description", sa.String(1000)),
        sa.Column("effective_description", sa.String(1000)),
    )


def downgrade():
    op.drop_table("l5_devops_space_intents")
