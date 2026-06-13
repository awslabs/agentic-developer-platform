"""Add index_run_stages table and repurpose index_runs as run header.

Issue #1423: Per-stage indexing tracking with verify-after-write.
Replaces the lying state file / DynamoDB approach with per-stage rows in Postgres,
each marked complete only after its output artifact is verified to exist (read-back).

Changes:
- Creates `index_run_stages` table (per-stage tracking with verification)
- Drops unused `steps_completed` JSONB from `index_runs` (wrong shape, never used)
- Adds `commit_sha` column to `index_runs` for SHA-based skip logic

Revision ID: 003_index_run_stages
Revises: 002_add_wiki_columns
Create Date: 2026-06-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "003_index_run_stages"
down_revision: str = "002_add_wiki_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create index_run_stages table and repurpose index_runs."""
    # -------------------------------------------------------------------------
    # Repurpose index_runs: add commit_sha, drop unused steps_completed JSONB
    # -------------------------------------------------------------------------
    op.execute("""
        ALTER TABLE index_runs
        ADD COLUMN commit_sha VARCHAR(40)
    """)

    op.execute("""
        ALTER TABLE index_runs
        DROP COLUMN steps_completed
    """)

    # -------------------------------------------------------------------------
    # index_run_stages — per-stage tracking with verify-after-write
    # -------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE index_run_stages (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id        UUID NOT NULL REFERENCES index_runs(id) ON DELETE CASCADE,
            repo          TEXT NOT NULL,
            stage         TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            artifact_ref  TEXT,
            verified_at   TIMESTAMPTZ,
            attempts      INT NOT NULL DEFAULT 0,
            error         TEXT,
            started_at    TIMESTAMPTZ,
            completed_at  TIMESTAMPTZ
        )
    """)

    # Indexes for common query patterns
    op.create_index("ix_index_run_stages_repo", "index_run_stages", ["repo"])
    op.create_index(
        "ix_index_run_stages_stage_status", "index_run_stages", ["stage", "status"]
    )
    op.create_index("ix_index_run_stages_run_id", "index_run_stages", ["run_id"])


def downgrade() -> None:
    """Remove index_run_stages and restore index_runs."""
    op.drop_index("ix_index_run_stages_run_id", table_name="index_run_stages")
    op.drop_index("ix_index_run_stages_stage_status", table_name="index_run_stages")
    op.drop_index("ix_index_run_stages_repo", table_name="index_run_stages")
    op.drop_table("index_run_stages")

    op.execute("ALTER TABLE index_runs ADD COLUMN steps_completed JSONB DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE index_runs DROP COLUMN commit_sha")
