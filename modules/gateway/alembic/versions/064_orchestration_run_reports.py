"""Durable scoped worker reporting for the shared-role dispatcher."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "064_orchestration_run_reports"
down_revision = "063_pr_binding_adoption"
branch_labels = None
depends_on = None


def upgrade():
    doc = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "orchestration_run_reports",
        sa.Column("run_id", sa.String(255), primary_key=True),
        sa.Column("credential_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("flow_id", sa.String(36), sa.ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("orchestration_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("persona", sa.String(64), nullable=False),
        sa.Column("repo", sa.String(255), nullable=False),
        sa.Column("installation_id", sa.Integer(), nullable=False),
        sa.Column("provider_repository_id", sa.BigInteger()),
        sa.Column("dispatch_metadata", doc, nullable=False),
        *[sa.Column(name, doc) for name in ("candidate_pr", "binding_receipt", "worker_receipt", "terminal_receipt", "review_receipt")],
        sa.Column("block_code", sa.String(80)),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_orchestration_run_reports_node", "orchestration_run_reports", ["org_id", "node_id", "attempt"])


def downgrade():
    op.drop_table("orchestration_run_reports")
