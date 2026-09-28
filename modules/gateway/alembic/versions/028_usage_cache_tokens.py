"""Add cache_read_input_tokens and cache_creation_input_tokens to usage_logs.

Issue #4180: prompt-cache token accounting. Before this migration the platform
recorded how many tokens a request used but not how many were cache READS vs.
fresh cache WRITES, so no query could distinguish "caching is working" from
"caching has never worked once" — which is precisely why the companion defect
(``/v1/messages`` silently stripping client ``cache_control`` breakpoints) went
unnoticed. The counters are what make cache effectiveness observable.

Both columns are NULLABLE with NO DEFAULT and NO BACKFILL, deliberately:

- ``NULL`` means "the provider did not report this counter" (or the row predates
  the feature). ``0`` means "the provider reported zero cache activity". Those
  are different facts, and a DEFAULT 0 would fuse them — making the hit-rate
  query silently wrong in the exact direction that hides the bug.
- A non-nullable column on this table would fail every in-flight INSERT on the
  usage hot path during rollout.

No index. The reporting query is a windowed ``SUM`` aggregate over a
high-write table, which an index on the summed columns does not help; contrast
``018``'s partial index on ``agent_run_id``, which exists because that column is
a point-lookup key.

Revision ID: 028_usage_cache_tokens
Revises: 027_install_tenant_unique
Create Date: 2026-08-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "028_usage_cache_tokens"
down_revision: str | None = "027_install_tenant_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the two nullable prompt-cache token columns to usage_logs."""
    op.execute("""
        ALTER TABLE usage_logs
        ADD COLUMN cache_read_input_tokens INTEGER NULL
    """)
    op.execute("""
        ALTER TABLE usage_logs
        ADD COLUMN cache_creation_input_tokens INTEGER NULL
    """)


def downgrade() -> None:
    """Remove the prompt-cache token columns from usage_logs.

    Present for CI/local upgrade-downgrade parity. Do NOT run this during an
    incident: the columns are nullable and ignored by pre-#4180 code, so a code
    revert alone fully restores previous behaviour. Dropping them destroys
    already-collected cache accounting for no benefit.
    """
    op.drop_column("usage_logs", "cache_creation_input_tokens")
    op.drop_column("usage_logs", "cache_read_input_tokens")
