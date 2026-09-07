"""Create person_budget_defaults — DEFAULT person limits at platform/org/team scope.

Issue #4690 (person-limits · D1).

**What this table is, and what it is not.** ``person_budget_configs`` (migration
034) stores one *person's* limit. This stores a **rule**: "$1,000/month each,
unless we say otherwise". One row governs every current and future member of its
scope. The rejected alternative was a bulk write — a platform admin typing the
same number into every person's row — which is not the same thing: it leaves
every future joiner unlimited, and it destroys any record of the intent (a
thousand individual rows are indistinguishable from a thousand individual
decisions).

**Defaults are CEILINGS** (operator ruling, 2026-09-07). The ladder in
``src/budget/person_ledger.py`` resolves individual row > team default > org
default > platform default; a person may set themselves a LOWER personal limit,
and only a platform admin may author an individual row above the applicable
default. None of that is expressible in DDL — this migration just gives those
rules somewhere to live.

Shape notes, each asserted by ``tests/migrations/test_036_person_budget_defaults.py``:

  - **No ``org_id`` column and no ``TenantMixin``**, same as 034 and for a
    stronger reason: the platform rung has no tenant at all. The org/team rungs
    name their tenant in ``scope_id_org`` explicitly, which is a scope the row
    *declares* rather than a partition it *lives in*.
  - **``scope_type`` is stored, not inferred** from which scope columns are NULL.
    A row states its rung, so adding a department rung later (#4690's non-goals
    leave room) is a new value here rather than a re-reading of existing rows.
  - **Two nullable scope columns, not one packed id.** A ``teams.id`` is unique
    only inside its org (``teams`` carries ``TenantMixin``), so the team rung needs
    both; and a packed ``"org:team"`` string would put two identifier namespaces
    in one column, the #4344 collision class.
  - **``ck_person_budget_default_scope``** pins the shape per rung. Without it an
    ``org`` row with a NULL ``scope_id_org`` is a rule matching every tenant's
    members through a NULL comparison nobody wrote, and a ``platform`` row
    carrying a stray org id reads as tenant-scoped to a human and platform-wide to
    the ladder.
  - **Uniqueness is a UNIQUE EXPRESSION INDEX over ``COALESCE(col, '')``, NOT a
    ``UniqueConstraint``.** This is the one non-obvious choice here and it is
    load-bearing: in Postgres NULLs compare *distinct* inside a unique
    constraint, so ``UNIQUE (scope_type, scope_id_org, scope_id_team,
    period_type)`` — the shape the issue sketched — accepts TWO platform defaults
    for the same period. The rung then holds two conflicting numbers and which
    governs depends on row order. Coalescing the nullable columns to ``''`` inside
    the index gives the intended "one rule per (scope, period)" and has the
    database enforce it rather than whichever writer remembers to check.
  - **``enforcement_mode`` server-defaults to ``hard``**, unlike 034's ``soft``.
    034 defaulted soft because C3's UI had promised users nothing would be
    blocked; there is no equivalent pre-enforcement generation of *these* rows to
    keep a promise to, and a governance default that silently does not enforce is
    the #4511 inert-cap class at platform scale.
  - **No FK on ``authored_by_user_id``** — canonical ``users.id`` (the #4647 audit
    contract), no FK for the same tenant-lifecycle reason as 034.

No index on ``scope_id_org`` alone: every ladder read is by ``scope_type`` first
(``platform``, then the person's orgs, then their teams), so the unique index's
leading column already seeks it, and the table holds one row per scope per period
— tiny by construction.

**Purely additive.** Nothing is backfilled and no existing row changes meaning: an
install with no rows here behaves exactly as it does today, which is what makes
the rollback "stop reading it, then drop it".

Revision id is 26 chars, inside the ``alembic_version.version_num`` VARCHAR(32)
ceiling ``tests/migrations/test_revision_id_length.py`` guards (#4123). Chains onto
the real single head, ``035_budget_usage_entity_key``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "036_person_budget_defaults"
down_revision: str | Sequence[str] | None = "035_budget_usage_entity_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "person_budget_defaults"
UNIQUE_INDEX = "uq_person_budget_default"
SCOPE_CHECK = "ck_person_budget_default_scope"

# One expression per rung, spelled out rather than compressed: each disjunct is a
# scope shape a reader can check against the ladder's matching code.
_SCOPE_SHAPE = (
    "(scope_type = 'platform' AND scope_id_org IS NULL AND scope_id_team IS NULL) "
    "OR (scope_type = 'org' AND scope_id_org IS NOT NULL AND scope_id_team IS NULL) "
    "OR (scope_type = 'team' AND scope_id_org IS NOT NULL AND scope_id_team IS NOT NULL)"
)


def upgrade() -> None:
    """Create the defaults table. See the module docstring for every shape choice."""
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        # Nullable: the platform rung has no tenant. NOT a foreign key to
        # `organizations` — this row is a rule about people, and an `ondelete`
        # would tie it to a tenant lifecycle (same reasoning as 034's missing FK).
        sa.Column("scope_id_org", sa.String(length=255), nullable=True),
        sa.Column("scope_id_team", sa.String(length=255), nullable=True),
        sa.Column("period_type", sa.String(length=10), nullable=False),
        # NUMERIC(10,2), byte-for-byte person_budget_configs.budget_amount_usd: the
        # ladder compares the two columns and reports whichever applies, so a
        # precision difference between them would be a silent rounding difference
        # between "your limit" and "the default you are held to".
        sa.Column("budget_amount_usd", sa.Numeric(precision=10, scale=2), nullable=False),
        # server_default as well as the model default, so a row inserted by
        # anything other than the ORM (a migration, an operator's psql session)
        # cannot land NULL — neither soft nor hard, and a reader would have to guess.
        sa.Column("enforcement_mode", sa.String(length=10), nullable=False, server_default="hard"),
        sa.Column("authored_by_user_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_SCOPE_SHAPE, name=SCOPE_CHECK),
        # Defaults are ALWAYS hard (review fix on #4696): headroom and the deny
        # path both rely on it, and writer-side constants cannot bind a psql
        # session or a future backfill. A soft default would render 'capped'
        # while warning-only — a silent platform-scale inert cap. Same
        # database-level standard as the scope-shape CHECK above.
        sa.CheckConstraint("enforcement_mode = 'hard'", name="ck_person_budget_default_hard"),
    )
    # NOT a UniqueConstraint — Postgres treats NULLs as distinct inside one, which
    # would let two platform defaults coexist for the same period. See the module
    # docstring.
    op.create_index(
        UNIQUE_INDEX,
        TABLE,
        ["scope_type", sa.text("COALESCE(scope_id_org, '')"), sa.text("COALESCE(scope_id_team, '')"), "period_type"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the table.

    Trivially reversible because nothing references it: no FK points at it, no
    existing row's meaning depends on it, and with the table gone the ladder's
    fallback simply finds no default — which is the pre-#4690 behaviour, where "no
    personal row" meant unlimited. The operational rollback is still "revert the
    PR"; this exists so ``alembic downgrade`` walks past the revision cleanly.
    """
    op.drop_index(UNIQUE_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
