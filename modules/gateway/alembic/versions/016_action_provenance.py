"""Create action_provenance table with pgcrypto extension.

Issue #784: Phase 2-a storage primitives for agent identity + action provenance.
Records every agent/human action for correlation tracking. Schema-only — no
runtime reads/writes until Phase 2-b/c/d.

Revision ID: 016_action_provenance
Revises: 015_bot_users
Create Date: 2026-05-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "016_action_provenance"
down_revision: str | None = "015_bot_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create pgcrypto extension and action_provenance table with indexes."""
    # pgcrypto provides gen_random_uuid() as a belt-and-suspenders default
    # for raw SQL inserts. Normal SQLAlchemy inserts use Python-side uuid4().
    # Idempotent — safe to run if already enabled. RDS allows without superuser.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute("""
        CREATE TABLE action_provenance (
            id              VARCHAR(255) PRIMARY KEY DEFAULT gen_random_uuid()::text,
            actor_user_id   VARCHAR(255) NOT NULL REFERENCES users(id),
            triggered_by    VARCHAR(255) REFERENCES users(id),
            root_human_id   VARCHAR(255) NOT NULL REFERENCES users(id),
            is_human_rooted BOOLEAN NOT NULL DEFAULT TRUE,
            action_kind     VARCHAR(64) NOT NULL,
            source_event    JSONB NOT NULL,
            correlation_id  VARCHAR(64) NOT NULL,
            org_id          VARCHAR(255) NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.create_index(
        "ix_action_provenance_root_human",
        "action_provenance",
        ["root_human_id"],
    )
    op.create_index(
        "ix_action_provenance_correlation",
        "action_provenance",
        ["correlation_id"],
    )
    op.create_index(
        "ix_action_provenance_actor",
        "action_provenance",
        ["actor_user_id"],
    )
    # TenantMixin adds org_id index via SQLAlchemy model metadata;
    # for the raw DDL path, the model's index=True on org_id handles it
    # at create_all() time. No explicit index needed here — avoids duplication.


def downgrade() -> None:
    """Drop action_provenance table and pgcrypto extension."""
    op.drop_index("ix_action_provenance_actor", table_name="action_provenance")
    op.drop_index("ix_action_provenance_correlation", table_name="action_provenance")
    op.drop_index("ix_action_provenance_root_human", table_name="action_provenance")
    op.drop_table("action_provenance")
    # Note: not dropping pgcrypto — other tables/extensions may depend on it,
    # and it's harmless to leave enabled.
