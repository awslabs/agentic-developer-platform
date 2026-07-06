"""Widen vulnerabilities columns from VARCHAR to TEXT.

Issue #2554: The vuln-scan upsert fails with StringDataRightTruncation because
OSV/Trivy produce affected_versions strings (long version-range lists across
many ecosystems) that exceed the original VARCHAR(512) limit. Widening to TEXT
removes the length constraint entirely — Postgres TEXT has no performance
penalty vs VARCHAR for B-tree indexes.

Columns changed:
- package:           VARCHAR(512) → TEXT
- affected_versions: VARCHAR(512) → TEXT
- safe_version:      VARCHAR(128) → TEXT

Revision ID: 011_widen_vuln_text_cols
Revises: 010_nullable_index_runs_repo_id
Create Date: 2026-06-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "011_widen_vuln_text_cols"
down_revision: str = "010_nullable_index_runs_repo_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen vulnerabilities text columns to TEXT (no length limit)."""
    op.execute("""
        ALTER TABLE vulnerabilities
        ALTER COLUMN package TYPE TEXT,
        ALTER COLUMN affected_versions TYPE TEXT,
        ALTER COLUMN safe_version TYPE TEXT
    """)


def downgrade() -> None:
    """Revert to original VARCHAR lengths (will fail if data exceeds limits)."""
    op.execute("""
        ALTER TABLE vulnerabilities
        ALTER COLUMN package TYPE VARCHAR(512),
        ALTER COLUMN affected_versions TYPE VARCHAR(512),
        ALTER COLUMN safe_version TYPE VARCHAR(128)
    """)
