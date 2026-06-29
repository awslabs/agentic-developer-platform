"""Make index_runs.repo_id nullable for non-repo asset stage tracking.

Issue #2308: URL and document ingestion workers need to create index_runs rows
but don't have entries in the repositories table. Making repo_id nullable allows
these non-repo assets to use the same stage tracking machinery as repos.

The FK constraint is preserved — when repo_id IS provided, it must reference a
valid repositories row. When NULL, it means this run belongs to a non-repo asset
(URL/doc) whose identity is tracked via index_run_stages.repo = registry_asset_id.

Revision ID: 010_nullable_index_runs_repo_id
Revises: 009_stage_metrics_and_pod
Create Date: 2026-06-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "010_nullable_index_runs_repo_id"
down_revision: str = "009_stage_metrics_and_pod"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make index_runs.repo_id nullable for non-repo assets."""
    op.execute("""
        ALTER TABLE index_runs
        ALTER COLUMN repo_id DROP NOT NULL
    """)


def downgrade() -> None:
    """Restore NOT NULL on index_runs.repo_id (will fail if NULL rows exist)."""
    op.execute("""
        ALTER TABLE index_runs
        ALTER COLUMN repo_id SET NOT NULL
    """)
