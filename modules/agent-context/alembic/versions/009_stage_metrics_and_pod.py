"""Add metrics and worker_pod columns to index_run_stages.

Issue #2305: Persist per-stage metrics (symbols/nodes/edges/etc.) and the
worker pod name for each ingestion stage so the detailed ingestion view
can show what each stage produced and which pod ran it (for log lookup).

Revision ID: 009_stage_metrics_pod
Revises: 008_ka_registry_cols
Create Date: 2026-06-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "009_stage_metrics_pod"
down_revision: str = "008_ka_registry_cols"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add metrics JSONB and worker_pod TEXT to index_run_stages."""
    op.execute("""
        ALTER TABLE index_run_stages
            ADD COLUMN IF NOT EXISTS metrics JSONB,
            ADD COLUMN IF NOT EXISTS worker_pod TEXT
    """)


def downgrade() -> None:
    """Remove metrics and worker_pod columns."""
    op.drop_column("index_run_stages", "worker_pod")
    op.drop_column("index_run_stages", "metrics")
