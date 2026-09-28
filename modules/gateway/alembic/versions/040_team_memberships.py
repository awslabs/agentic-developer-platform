"""Create team_memberships table + backfill from users.team_id.

Issue #4840 (EPIC #4839) — C1 of the accepted design
(`docs/design-notes/4828-platform-native-org-team-user.md` §2.1, ruling R2):
many-to-many user<->team membership with at most one primary per user per org.

Additive and fully reversible. ``users.team_id`` is deliberately left in place and
unchanged: it stays the denormalized primary-team pointer that the Cognito
pre-token Lambda and every ``custom:team_id`` consumer already read, so nothing
downstream has to change in lockstep with this migration. Rollback is
``downgrade()`` — dropping the table loses no truth, because ``users.team_id``
still carries the primary.

Two mechanics here are load-bearing and easy to get wrong:

1. **The partial unique index is PostgreSQL-only** (design note §1.5b). SQLite has
   no usable equivalent for our purposes, and `Base.metadata.create_all` — how the
   test suite builds its schema — silently ignores `postgresql_where` and would
   build a *plain* unique index on `(user_id, org_id)`, which wrongly rejects a
   user's second (non-primary) team. So it is created behind an explicit dialect
   guard, exactly as migration `021:52-54` does for its one-active-per-user index.
   Because CI therefore cannot enforce one-primary at the DB level, the invariant
   ALSO lives in the application layer (`src/admin/team_memberships.py`), and
   `tests/migrations/test_040_team_memberships.py` asserts the real Postgres DDL
   from this file's source.

   Note this uses a plain inline `CREATE UNIQUE INDEX`, not `CONCURRENTLY` +
   `autocommit_block()`. `CONCURRENTLY` avoids locking a table that has live
   readers; this table is created three statements earlier in the same migration,
   so there are none, and `CONCURRENTLY` cannot run inside a transaction anyway.
   Migration 027 needed that machinery because it indexed the pre-existing, live
   `channel_tenant_map`.

2. **The backfill must skip empty-string team ids** (design note §1.5e).
   ``users.team_id`` is NOT NULL but three live writers mint it as ``""`` —
   ``src/internal/routes.py:349`` and ``:359`` (shadow users, which have no team
   until claimed) and ``src/admin/onboarding/approval.py:83``. ``""`` is not a
   valid ``teams.id``, so inserting a membership row for those users would violate
   the FK and fail the migration, taking the whole gateway deploy with it. Those
   users correctly get NO membership row.

Revision ID: 040_team_memberships
Revises: 039_flow_design_capture
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "040_team_memberships"
down_revision: str | None = "039_flow_design_capture"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Named here so the migration, the ORM model, and the test all reference one string.
ONE_PRIMARY_INDEX = "uq_team_memberships_one_primary"


def upgrade() -> None:
    """Create team_memberships, its indexes, and backfill from users.team_id."""

    # -- 1. Create team_memberships table --
    op.create_table(
        "team_memberships",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("team_id", sa.String(length=255), nullable=False),
        sa.Column("org_id", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="member"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="admin"),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "team_id", name="uq_team_memberships_user_team"),
    )

    # Standard lookup indexes: "which teams is this user on" and "who is on this
    # team" are both hot paths for the admin UI (T2) and team-scoped budgets.
    op.create_index("ix_team_memberships_user_id", "team_memberships", ["user_id"])
    op.create_index("ix_team_memberships_team_id", "team_memberships", ["team_id"])
    op.create_index("ix_team_memberships_org_id", "team_memberships", ["org_id"])

    # -- 2. Partial unique index: at most one primary per user per org (Postgres only) --
    # See module docstring point 1. Enforced in the application layer everywhere else.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(f"CREATE UNIQUE INDEX {ONE_PRIMARY_INDEX} ON team_memberships (user_id, org_id) WHERE is_primary"))

    # -- 3. Idempotent backfill: one primary membership per user with a real team --
    # See module docstring point 2 for why team_id != '' is mandatory, not defensive.
    if bind.dialect.name == "postgresql":
        uuid_expr = "gen_random_uuid()::text"
    else:
        # SQLite: generate UUID v4 using randomblob (mirrors migration 021).
        uuid_expr = (
            "lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' || "
            "substr(hex(randomblob(2)),2) || '-' || "
            "substr('89ab', abs(random()) % 4 + 1, 1) || "
            "substr(hex(randomblob(2)),2) || '-' || hex(randomblob(6)))"
        )

    op.execute(
        sa.text(
            f"INSERT INTO team_memberships (id, user_id, team_id, org_id, role, is_primary, source) "
            f"SELECT "
            f"  {uuid_expr}, "
            f"  u.id, u.team_id, u.org_id, 'member', true, 'admin' "
            f"FROM users u "
            f"WHERE u.team_id != '' "
            # Only teams that actually exist: a stale pointer to a deleted team
            # would fail the FK and take the deploy down with it.
            f"  AND EXISTS (SELECT 1 FROM teams t WHERE t.id = u.team_id) "
            f"  AND NOT EXISTS ("
            f"    SELECT 1 FROM team_memberships tm "
            f"    WHERE tm.user_id = u.id AND tm.team_id = u.team_id"
            f"  )"
        )
    )


def downgrade() -> None:
    """Drop team_memberships. Data-preserving: users.team_id still holds the primary."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(f"DROP INDEX IF EXISTS {ONE_PRIMARY_INDEX}"))
    op.drop_index("ix_team_memberships_org_id", table_name="team_memberships")
    op.drop_index("ix_team_memberships_team_id", table_name="team_memberships")
    op.drop_index("ix_team_memberships_user_id", table_name="team_memberships")
    op.drop_table("team_memberships")
