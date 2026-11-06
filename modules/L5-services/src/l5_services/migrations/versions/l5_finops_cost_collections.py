"""Retain scoped CE collection checkpoints after native/provider cleanup.

Revision ID: l5_finops_cost_collections
Revises: l5_finops_inquiry_retry_merge
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "l5_finops_cost_collections"
down_revision = "l5_finops_inquiry_retry_merge"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "l5_service_cost_collections",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("collection_id", sa.String(36), primary_key=True),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("checkpoint_ref", payload),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("coverage", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("supersedes_id", sa.String(36)),
        sa.CheckConstraint("request_count >= 0 AND page_count >= 0 AND page_count <= request_count",
                           name="ck_l5_cost_collection_counts"),
        sa.CheckConstraint("status IN ('collecting', 'complete', 'partial', 'superseded')",
                           name="ck_l5_cost_collection_status"),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"],
                                ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
    )
    op.create_index("ix_l5_cost_collection_run", "l5_service_cost_collections",
                    ["tenant_id", "run_id", "status"])


def downgrade():
    op.drop_index("ix_l5_cost_collection_run", table_name="l5_service_cost_collections")
    op.drop_table("l5_service_cost_collections")
