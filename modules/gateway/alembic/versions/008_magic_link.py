"""Add magic_link_nonces, security_audit_logs tables; add is_shadow to users.

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
    """Add magic-link infrastructure: nonces table, security_audit_logs table, is_shadow column.

    Idempotent: skips any create that has already been applied. A previous
    attempt at 008 (when security_audit_logs was still named audit_logs and
    collided with admin's audit_logs) may have created magic_link_nonces
    before aborting, leaving the migration version unset.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # -- magic_link_nonces --
    if "magic_link_nonces" not in existing_tables:
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
        existing_indexes = set()
    else:
        existing_indexes = {i["name"] for i in inspector.get_indexes("magic_link_nonces")}
    if "ix_magic_link_nonces_expires_at" not in existing_indexes:
        op.create_index("ix_magic_link_nonces_expires_at", "magic_link_nonces", ["expires_at"])

    # -- security_audit_logs --
    if "security_audit_logs" not in existing_tables:
        op.create_table(
            "security_audit_logs",
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
        existing_audit_indexes = set()
    else:
        existing_audit_indexes = {i["name"] for i in inspector.get_indexes("security_audit_logs")}
    for col in ("org_id", "event_type", "actor_id", "created_at"):
        idx_name = f"ix_security_audit_logs_{col}"
        if idx_name not in existing_audit_indexes:
            op.create_index(idx_name, "security_audit_logs", [col])

    # -- users.is_shadow --
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

    op.drop_index("ix_security_audit_logs_created_at", table_name="security_audit_logs")
    op.drop_index("ix_security_audit_logs_actor_id", table_name="security_audit_logs")
    op.drop_index("ix_security_audit_logs_event_type", table_name="security_audit_logs")
    op.drop_index("ix_security_audit_logs_org_id", table_name="security_audit_logs")
    op.drop_table("security_audit_logs")

    op.drop_index("ix_magic_link_nonces_expires_at", table_name="magic_link_nonces")
    op.drop_table("magic_link_nonces")
