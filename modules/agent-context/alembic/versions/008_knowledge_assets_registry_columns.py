"""Add status_detail, display_name, tags, installation_id to knowledge_assets.

Issue #2182: The gateway registry API (routes.py) INSERTs into display_name,
tags, and installation_id columns, and the status-callback endpoint writes
status_detail. These columns were defined in gateway migrations 019/020 (which
targeted the wrong database and never ran). This migration adds them to the
authoritative table in agent_context.

Revision ID: 008_knowledge_assets_registry_columns
Revises: 007_knowledge_assets
Create Date: 2026-06-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "008_knowledge_assets_registry_columns"
down_revision: str = "007_knowledge_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add registry columns needed by gateway API and status-callback."""
    op.execute("""
        ALTER TABLE knowledge_assets
            ADD COLUMN IF NOT EXISTS status_detail JSONB,
            ADD COLUMN IF NOT EXISTS display_name VARCHAR(512),
            ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS installation_id BIGINT
    """)


def downgrade() -> None:
    """Remove registry columns."""
    op.drop_column("knowledge_assets", "installation_id")
    op.drop_column("knowledge_assets", "tags")
    op.drop_column("knowledge_assets", "display_name")
    op.drop_column("knowledge_assets", "status_detail")
