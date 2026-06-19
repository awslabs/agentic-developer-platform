"""Add agent_run_id and chat_log_s3_key columns to usage_logs.

Issue #1616: Per-run cost + S3 traceability — correlate Bedrock calls
to agent runs, surface price + payload link in Agent Activity.

- agent_run_id: nullable, indexed — the invocation_id (= message_id)
  from the agent-worker, carried via X-Agent-RunId header.
- chat_log_s3_key: nullable — the S3 object key for the request/response
  payload, written by the budget-usage-tracker Lambda when it bridges cost.

Nullable, no backfill — existing rows stay null (pre-feature calls have
no run identifier; non-gateway-mode calls will also remain null).

Revision ID: 018_agent_run_cost_traceability
Revises: 017_parent_invocation_id
Create Date: 2026-06-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "018_agent_run_cost_traceability"
down_revision: str | None = "017_parent_invocation_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add agent_run_id (indexed) and chat_log_s3_key columns to usage_logs."""
    op.execute("""
        ALTER TABLE usage_logs
        ADD COLUMN agent_run_id VARCHAR(255) NULL
    """)
    op.execute("""
        ALTER TABLE usage_logs
        ADD COLUMN chat_log_s3_key VARCHAR(1024) NULL
    """)
    op.execute("""
        CREATE INDEX ix_usage_logs_agent_run_id
        ON usage_logs (agent_run_id)
        WHERE agent_run_id IS NOT NULL
    """)


def downgrade() -> None:
    """Remove agent_run_id and chat_log_s3_key columns from usage_logs."""
    op.execute("DROP INDEX IF EXISTS ix_usage_logs_agent_run_id")
    op.drop_column("usage_logs", "chat_log_s3_key")
    op.drop_column("usage_logs", "agent_run_id")
