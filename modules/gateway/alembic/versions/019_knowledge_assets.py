"""Tombstone for the removed knowledge_assets migration (no-op).

Issue #2182 / PR #2188 consolidated the Knowledge registry into the
`agent_context` database and DELETED the original gateway migrations
019_knowledge_assets + 020 (they created the table in the WRONG db,
`bedrockgateway`, and never ran successfully). However, the live
`bedrockgateway` database was already stamped at `019_knowledge_assets`,
so after the deletion alembic failed with:

    Can't locate revision identified by '019_knowledge_assets'

blocking ALL gateway migrations (and the gateway deploy / frontend rollout).

This re-introduces revision `019_knowledge_assets` as a pure **no-op tombstone**
so alembic can resolve the stamped revision and the chain stays linear. It does
NOT recreate the knowledge_assets table — that table is owned by agent_context
now (agent-context migration 007 + 008_ka_registry_cols). Forward-only.

Revision ID: 019_knowledge_assets
Revises: 018_agent_run_cost_traceability
Create Date: 2026-06-27
"""

from collections.abc import Sequence

revision: str = "019_knowledge_assets"
down_revision: str = "018_agent_run_cost_traceability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op. The knowledge_assets table lives in the agent_context DB now."""
    pass


def downgrade() -> None:
    """No-op."""
    pass
