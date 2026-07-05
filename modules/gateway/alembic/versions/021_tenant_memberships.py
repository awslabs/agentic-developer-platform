"""Create tenant_memberships table + backfill + relax user_identities index.

Issue #2961: D5 data foundation — tenant_memberships table with one-active-per-user
guarantee, idempotent backfill for existing users, and relaxed identity uniqueness
to allow one GitHub user in multiple tenants.

Revision ID: 021_tenant_memberships
Revises: 020_org_github_ids
Create Date: 2026-07-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "021_tenant_memberships"
down_revision: str = "020_org_github_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create tenant_memberships table, backfill existing users, relax user_identities index."""

    # -- 1. Create tenant_memberships table --
    op.create_table(
        "tenant_memberships",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="member"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("joined_via", sa.String(length=32), nullable=False, server_default="org_membership"),
        sa.Column("github_org_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "tenant_id", name="uq_tenant_memberships_user_tenant"),
    )

    # Standard lookup indexes
    op.create_index("ix_tenant_memberships_tenant_id", "tenant_memberships", ["tenant_id"])
    op.create_index("ix_tenant_memberships_user_id", "tenant_memberships", ["user_id"])

    # Partial unique index: at most one active membership per user (PostgreSQL only).
    # SQLite does not support partial indexes — the constraint is enforced at
    # the application level in tests.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("CREATE UNIQUE INDEX uq_tenant_memberships_one_active ON tenant_memberships (user_id) WHERE is_active = true"))

    # -- 2. Idempotent backfill: one membership per existing user --
    if bind.dialect.name == "postgresql":
        uuid_expr = "gen_random_uuid()::text"
    else:
        # SQLite: generate UUID v4 using randomblob
        uuid_expr = (
            "lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' || "
            "substr(hex(randomblob(2)),2) || '-' || "
            "substr('89ab', abs(random()) % 4 + 1, 1) || "
            "substr(hex(randomblob(2)),2) || '-' || hex(randomblob(6)))"
        )

    op.execute(
        sa.text(
            f"INSERT INTO tenant_memberships (id, user_id, tenant_id, role, is_active, joined_via) "
            f"SELECT "
            f"  {uuid_expr}, "
            f"  u.id, u.org_id, COALESCE(u.role, 'member'), true, 'username_self' "
            f"FROM users u "
            f"WHERE NOT EXISTS ("
            f"  SELECT 1 FROM tenant_memberships tm "
            f"  WHERE tm.user_id = u.id AND tm.tenant_id = u.org_id"
            f")"
        )
    )

    # -- 3. Relax user_identities unique index --
    # Drop old: UNIQUE (provider, provider_user_id)
    op.drop_index("uq_user_identities_provider_provider_user_id", table_name="user_identities")
    # Create new: UNIQUE (provider, provider_user_id, org_id) — unique per provider per tenant
    op.create_index(
        "uq_user_identities_provider_provider_user_id_org_id",
        "user_identities",
        ["provider", "provider_user_id", "org_id"],
        unique=True,
    )


def downgrade() -> None:
    """Reverse: restore old user_identities index, drop tenant_memberships."""

    # -- Restore user_identities unique index --
    op.drop_index("uq_user_identities_provider_provider_user_id_org_id", table_name="user_identities")
    op.create_index(
        "uq_user_identities_provider_provider_user_id",
        "user_identities",
        ["provider", "provider_user_id"],
        unique=True,
    )

    # -- Drop tenant_memberships --
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP INDEX IF EXISTS uq_tenant_memberships_one_active"))
    op.drop_index("ix_tenant_memberships_user_id", table_name="tenant_memberships")
    op.drop_index("ix_tenant_memberships_tenant_id", table_name="tenant_memberships")
    op.drop_table("tenant_memberships")
