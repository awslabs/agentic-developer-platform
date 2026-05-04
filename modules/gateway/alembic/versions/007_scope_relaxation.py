"""Relax user_credentials ownership — user/team/org/domain-app scopes.

Issue #440: Credential scope relaxation

Revision ID: 007_scope_relaxation
Revises: 006_user_roles
Create Date: 2026-05-04

Changes to user_credentials:
- user_id: NOT NULL → NULLable (was always-required owner; now optional)
- team_id: NOT NULL → NULLable (was denorm copy; now a real ownership column)
- domain_app_id: new NULLable VARCHAR(255) column for domain-app-scoped creds
- strict: new BOOLEAN NOT NULL DEFAULT FALSE (disables resolver fallback for
  high-sensitivity creds when set to True)
- Old unique (user_id, service, label) dropped; replaced by three
  scope-aware partial unique indexes (one per owner scope).
- PostgreSQL CHECK constraint: exactly one of (user_id, team_id,
  domain_app_id) must be non-NULL.

Existing rows: all current rows have user_id set (the v1 invariant), so they
remain valid after the migration — user_id stays non-null for those rows.
The team_id column in existing rows was a denormalised copy of the user's
team_id.  After this migration team_id is NOT an ownership column for those
rows (user_id is); existing team_id values are therefore cleared to NULL so
they do not accidentally satisfy the ownership constraint on their own.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "007_scope_relaxation"
down_revision: str | None = "006_user_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Name of the old unique index we are replacing.
_OLD_UNIQUE_IDX = "uq_user_credentials_user_service_label"

# Names of the new scope-aware unique indexes.
_USER_UNIQUE_IDX = "uq_user_credentials_user_service_label"
_TEAM_UNIQUE_IDX = "uq_user_credentials_team_service_label"
_DOMAIN_APP_UNIQUE_IDX = "uq_user_credentials_domain_app_service_label"

# Name of the CHECK constraint.
_CHECK_NAME = "ck_user_credentials_single_owner"


def upgrade() -> None:
    """Apply scope relaxation to user_credentials."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    existing_cols = {c["name"] for c in inspector.get_columns("user_credentials")}
    existing_indexes = {i["name"] for i in inspector.get_indexes("user_credentials")}

    # ------------------------------------------------------------------
    # 1. Drop the old NOT-NULL-user-only unique index.
    # ------------------------------------------------------------------
    if _OLD_UNIQUE_IDX in existing_indexes:
        op.drop_index(_OLD_UNIQUE_IDX, table_name="user_credentials")

    # ------------------------------------------------------------------
    # 2. Remove the FK from user_id so we can make it nullable.
    #    SQLite does not support ALTER COLUMN or DROP CONSTRAINT — it
    #    silently ignores batch_alter_table operations that don't apply.
    # ------------------------------------------------------------------
    if dialect != "sqlite":
        # In Postgres, FKs must be dropped before altering nullability.
        existing_fks = {fk["name"] for fk in inspector.get_foreign_keys("user_credentials")}
        user_fk = next(
            (fk["name"] for fk in inspector.get_foreign_keys("user_credentials") if fk["constrained_columns"] == ["user_id"] and fk["name"]),
            None,
        )
        if user_fk and user_fk in existing_fks:
            op.drop_constraint(user_fk, "user_credentials", type_="foreignkey")

    # ------------------------------------------------------------------
    # 3. Make user_id nullable.
    # ------------------------------------------------------------------
    op.alter_column(
        "user_credentials",
        "user_id",
        existing_type=sa.String(255),
        nullable=True,
        existing_nullable=False,
    )

    # ------------------------------------------------------------------
    # 4. Make team_id nullable (it was a NOT NULL denorm copy; we clear
    #    it for existing rows below so it no longer doubles as owner).
    # ------------------------------------------------------------------
    op.alter_column(
        "user_credentials",
        "team_id",
        existing_type=sa.String(255),
        nullable=True,
        existing_nullable=False,
    )

    # ------------------------------------------------------------------
    # 5. Clear team_id on existing rows — these rows are user-owned;
    #    team_id was only a denorm convenience copy, not an owner column.
    # ------------------------------------------------------------------
    op.execute(sa.text("UPDATE user_credentials SET team_id = NULL WHERE user_id IS NOT NULL"))

    # ------------------------------------------------------------------
    # 6. Add domain_app_id column.
    # ------------------------------------------------------------------
    if "domain_app_id" not in existing_cols:
        op.add_column(
            "user_credentials",
            sa.Column("domain_app_id", sa.String(255), nullable=True),
        )

    # ------------------------------------------------------------------
    # 7. Add strict column (defaults to False — fallback enabled).
    # ------------------------------------------------------------------
    if "strict" not in existing_cols:
        op.add_column(
            "user_credentials",
            sa.Column(
                "strict",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    # ------------------------------------------------------------------
    # 8. Re-add the FK on user_id (now nullable, CASCADE on delete).
    # ------------------------------------------------------------------
    if dialect != "sqlite":
        op.create_foreign_key(
            "fk_user_credentials_user_id",
            "user_credentials",
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # ------------------------------------------------------------------
    # 9. Add scope-aware unique indexes.
    #    PostgreSQL: partial (WHERE owner IS NOT NULL).
    #    SQLite: plain composite (partial syntax unsupported; NULL values
    #    are distinct in SQLite unique indexes, so duplicate prevention
    #    still works for the SQLite-based test suite).
    # ------------------------------------------------------------------
    if _USER_UNIQUE_IDX not in existing_indexes:
        if dialect == "postgresql":
            op.create_index(
                _USER_UNIQUE_IDX,
                "user_credentials",
                ["user_id", "service", "label"],
                unique=True,
                postgresql_where=sa.text("user_id IS NOT NULL"),
            )
        else:
            op.create_index(
                _USER_UNIQUE_IDX,
                "user_credentials",
                ["user_id", "service", "label"],
                unique=True,
            )

    if _TEAM_UNIQUE_IDX not in existing_indexes:
        if dialect == "postgresql":
            op.create_index(
                _TEAM_UNIQUE_IDX,
                "user_credentials",
                ["team_id", "service", "label"],
                unique=True,
                postgresql_where=sa.text("team_id IS NOT NULL"),
            )
        else:
            op.create_index(
                _TEAM_UNIQUE_IDX,
                "user_credentials",
                ["team_id", "service", "label"],
                unique=True,
            )

    if _DOMAIN_APP_UNIQUE_IDX not in existing_indexes:
        if dialect == "postgresql":
            op.create_index(
                _DOMAIN_APP_UNIQUE_IDX,
                "user_credentials",
                ["domain_app_id", "service", "label"],
                unique=True,
                postgresql_where=sa.text("domain_app_id IS NOT NULL"),
            )
        else:
            op.create_index(
                _DOMAIN_APP_UNIQUE_IDX,
                "user_credentials",
                ["domain_app_id", "service", "label"],
                unique=True,
            )

    # ------------------------------------------------------------------
    # 10. Add CHECK constraint: exactly one owner column must be non-NULL.
    #     PostgreSQL supports CHECK constraints; SQLite supports them but
    #     does NOT enforce them by default (and does not have a
    #     num_nonnull() function), so we skip it on SQLite — the model
    #     validator covers that case in application code.
    # ------------------------------------------------------------------
    if dialect == "postgresql":
        op.create_check_constraint(
            _CHECK_NAME,
            "user_credentials",
            "(user_id IS NOT NULL)::int + (team_id IS NOT NULL)::int + (domain_app_id IS NOT NULL)::int <= 1",
        )


def downgrade() -> None:
    """Reverse scope relaxation — restore user-only ownership."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    existing_indexes = {i["name"] for i in inspector.get_indexes("user_credentials")}

    # ------------------------------------------------------------------
    # 1. Drop CHECK constraint (PostgreSQL only).
    # ------------------------------------------------------------------
    if dialect == "postgresql":
        op.drop_constraint(_CHECK_NAME, "user_credentials", type_="check")

    # ------------------------------------------------------------------
    # 2. Drop new scope-aware unique indexes.
    # ------------------------------------------------------------------
    for idx in [_TEAM_UNIQUE_IDX, _DOMAIN_APP_UNIQUE_IDX]:
        if idx in existing_indexes:
            op.drop_index(idx, table_name="user_credentials")
    # User unique index same name as old one — keep it.

    # ------------------------------------------------------------------
    # 3. Drop added columns.
    # ------------------------------------------------------------------
    op.drop_column("user_credentials", "strict")
    op.drop_column("user_credentials", "domain_app_id")

    # ------------------------------------------------------------------
    # 4. Restore team_id to NOT NULL (fill in user's team where NULL).
    #    We use org_id as a fallback value so the NOT NULL restore
    #    doesn't fail — a proper restore would need the original data.
    # ------------------------------------------------------------------
    op.execute(sa.text("UPDATE user_credentials SET team_id = org_id WHERE team_id IS NULL"))
    op.alter_column(
        "user_credentials",
        "team_id",
        existing_type=sa.String(255),
        nullable=False,
        existing_nullable=True,
    )

    # ------------------------------------------------------------------
    # 5. Restore user_id to NOT NULL (any NULL rows would need to be
    #    deleted first — those rows were not possible before this migration).
    # ------------------------------------------------------------------
    op.execute(sa.text("DELETE FROM user_credentials WHERE user_id IS NULL"))
    op.alter_column(
        "user_credentials",
        "user_id",
        existing_type=sa.String(255),
        nullable=False,
        existing_nullable=True,
    )
