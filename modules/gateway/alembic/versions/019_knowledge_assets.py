"""Create knowledge_assets table in gateway DB.

Issue #2046 (#2039 I5): gateway-side home for the Knowledge Registry's
user-facing rows. Mirrors the column set from agent-context migration 007
plus a nullable `status_detail` JSONB column reserved for the
worker->gateway status-callback projection.

This is a CREATE-FRESH migration (NOT a data move) — the registry API 404s
in every deployment, so there is zero live data in the agent-context table.

Revision ID: 019_knowledge_assets
Revises: 018_agent_run_cost_traceability
Create Date: 2026-06-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "019_knowledge_assets"
down_revision: str | None = "018_agent_run_cost_traceability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create knowledge_assets table with indexes and unique constraint."""
    op.execute("""
        CREATE TABLE knowledge_assets (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

            -- What kind of asset (open VARCHAR, NOT an enum/CHECK)
            asset_type      VARCHAR(32) NOT NULL,

            -- The reference (what to index)
            source_ref      VARCHAR(2048) NOT NULL,

            -- Scope (who owns this registration) — values, not FKs
            tenant_id       VARCHAR(256),
            owner_sub       VARCHAR(256),
            project_id      UUID,

            -- Status lifecycle
            status          VARCHAR(32) NOT NULL DEFAULT 'pending',
            registered_by   VARCHAR(256),

            -- Timestamps
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            -- Extensibility
            metadata        JSONB DEFAULT '{}'::jsonb,

            -- Worker->gateway status-callback projection (day-one unused)
            status_detail   JSONB,

            -- Error tracking for retry logic
            last_error      TEXT,
            retry_count     INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Unique constraint: one registration per (source_ref, tenant_id, owner_sub)
    # Uses COALESCE to handle NULLs (NULLs are distinct in standard unique
    # constraints, which would allow duplicate registrations)
    op.execute("""
        CREATE UNIQUE INDEX uq_knowledge_assets_source_scope
        ON knowledge_assets (
            source_ref,
            COALESCE(tenant_id, ''),
            COALESCE(owner_sub, '')
        )
    """)

    # Composite index for scoped list queries (design requirement)
    op.create_index(
        "ix_knowledge_assets_tenant_owner",
        "knowledge_assets",
        ["tenant_id", "owner_sub"],
    )

    # Lookup indexes for common query patterns
    op.create_index(
        "ix_knowledge_assets_tenant_id",
        "knowledge_assets",
        ["tenant_id"],
        postgresql_where="tenant_id IS NOT NULL",
    )
    op.create_index(
        "ix_knowledge_assets_owner_sub",
        "knowledge_assets",
        ["owner_sub"],
        postgresql_where="owner_sub IS NOT NULL",
    )
    op.create_index(
        "ix_knowledge_assets_status",
        "knowledge_assets",
        ["status"],
    )
    op.create_index(
        "ix_knowledge_assets_project_id",
        "knowledge_assets",
        ["project_id"],
        postgresql_where="project_id IS NOT NULL",
    )


def downgrade() -> None:
    """Drop knowledge_assets table and all indexes."""
    op.drop_index("ix_knowledge_assets_project_id", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_status", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_owner_sub", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_tenant_id", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_tenant_owner", table_name="knowledge_assets")
    op.execute("DROP INDEX IF EXISTS uq_knowledge_assets_source_scope")
    op.drop_table("knowledge_assets")
