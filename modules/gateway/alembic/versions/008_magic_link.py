"""Add magic_link_nonces, audit_logs tables; add is_shadow to users.

Issue #446: Vault Phase 2b — Magic-link identity linking flow

Revision ID: 008_magic_link
Revises: 007_scope_relaxation
Create Date: 2026-05-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "008_magic_link"
down_revision: str | None = "007_scope_relaxation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add magic-link infrastructure: nonces table, audit_logs table, is_shadow column."""

    # -- magic_link_nonces --
    op.create_table(
        "magic_link_nonces",
        sa.Column("jti", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("channel_context", sa.String(length=512), nullable=True),
        sa.Column("target_user_id", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("jti"),
    )
    op.create_index("ix_magic_link_nonces_expires_at", "magic_link_nonces", ["expires_at"])

    # -- audit_logs --
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=255), nullable=False),
        sa.Column(
            "event_type",
            sa.String(length=64),
            nullable=False,
            comment="magic_link_issued | magic_link_consumed | magic_link_failed | identity_linked | shadow_user_created | identity_unlinked",
        ),
        sa.Column("actor_id", sa.String(length=255), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_org_id", "audit_logs", ["org_id"])
    op.create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"])
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    # -- users.is_shadow --
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("users")}
    if "is_shadow" not in existing_cols:
        op.add_column(
            "users",
            sa.Column(
                "is_shadow",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
                comment="True for auto-provisioned shadow users (from channel_tenant_map). Cannot log into ADP UI.",
            ),
        )


def downgrade() -> None:
    """Drop magic-link tables and is_shadow column."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("users")}
    if "is_shadow" in existing_cols:
        op.drop_column("users", "is_shadow")

    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_event_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_org_id", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_magic_link_nonces_expires_at", table_name="magic_link_nonces")
    op.drop_table("magic_link_nonces")
