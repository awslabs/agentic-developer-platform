"""Add research_proposals table for insight generation (US-G3).

Revision ID: 005_add_research_proposals
Revises: 004_jsonb_state_columns
Create Date: 2026-04-03

Adds:
  - research_proposals table with title, objective, hypothesis,
    source_findings (JSONB), cost/duration estimates, experiment_plan (JSONB),
    status workflow columns, and approval tracking.
  - Indexes on workspace_id, status, and created_at for efficient querying.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "005_add_research_proposals"
down_revision = "004_jsonb_state_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_proposals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=True,
        ),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("objective", sa.Text, nullable=False),
        sa.Column("hypothesis", sa.Text, nullable=False),
        sa.Column("source_findings", JSONB, nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column("estimated_duration_hours", sa.Numeric(6, 2), nullable=True),
        sa.Column("required_resources", sa.Text, nullable=True),
        sa.Column("experiment_plan", JSONB, nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="proposed",
        ),
        sa.Column("approved_by", sa.String(256), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_reason", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )

    # Indexes for common query patterns
    op.create_index(
        "ix_research_proposals_workspace_id",
        "research_proposals",
        ["workspace_id"],
    )
    op.create_index(
        "ix_research_proposals_status",
        "research_proposals",
        ["status"],
    )
    op.create_index(
        "ix_research_proposals_created_at",
        "research_proposals",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_proposals_created_at")
    op.drop_index("ix_research_proposals_status")
    op.drop_index("ix_research_proposals_workspace_id")
    op.drop_table("research_proposals")
