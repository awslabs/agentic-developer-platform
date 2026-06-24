"""Create knowledge_assets registry table.

Issue #1790 (Story A of E10 #1736): the single source of truth for
"what to index + scope". Foundation table consumed by the CRUD API (Story B),
bulk upload (Story C), trigger dispatch (Story H), and the Management UI
(Stories E-G).

Schema per design docs/agent-context/design-1736-knowledge-asset-registry.md S3.

Revision ID: 007_knowledge_assets
Revises: 006_merge_005_heads
Create Date: 2026-06-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "007_knowledge_assets"
down_revision: str = "006_merge_005_heads"
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

            -- Scope (who owns this registration)
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

            -- Error tracking for retry logic
            last_error      TEXT,
            retry_count     INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Unique constraint: one registration per (source_ref, tenant_id, owner_sub)
    # Uses COALESCE to handle NULLs in the unique index (NULLs are distinct in
    # standard unique constraints, which would allow duplicate registrations)
    op.execute("""
        CREATE UNIQUE INDEX uq_knowledge_assets_source_scope
        ON knowledge_assets (
            source_ref,
            COALESCE(tenant_id, ''),
            COALESCE(owner_sub, '')
        )
    """)

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
    op.execute("DROP INDEX IF EXISTS uq_knowledge_assets_source_scope")
    op.drop_table("knowledge_assets")
