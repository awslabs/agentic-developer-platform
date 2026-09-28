"""Add research_findings table for external data scanner (US-G2).

Revision ID: 003_add_research_findings
Revises: 002_add_research_workspace
Create Date: 2026-04-01

Adds:
  - research_findings table with source, title, summary,
    relevance_score, tags (JSONB), raw_content_json (JSONB),
    and timestamp columns.
  - Indexes on workspace_id, source, and relevance_score for
    efficient querying and filtering.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "003_add_research_findings"
down_revision = "002_add_research_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_findings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=True,
        ),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_url", sa.String(2048), nullable=False),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("relevance_score", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tags", JSONB, nullable=True),
        sa.Column("raw_content_json", JSONB, nullable=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )

    # Indexes for common query patterns
    op.create_index(
        "ix_research_findings_workspace_id",
        "research_findings",
        ["workspace_id"],
    )
    op.create_index(
        "ix_research_findings_source",
        "research_findings",
        ["source"],
    )
    op.create_index(
        "ix_research_findings_relevance_score",
        "research_findings",
        ["relevance_score"],
    )
    op.create_index(
        "ix_research_findings_scanned_at",
        "research_findings",
        ["scanned_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_findings_scanned_at")
    op.drop_index("ix_research_findings_relevance_score")
    op.drop_index("ix_research_findings_source")
    op.drop_index("ix_research_findings_workspace_id")
    op.drop_table("research_findings")
