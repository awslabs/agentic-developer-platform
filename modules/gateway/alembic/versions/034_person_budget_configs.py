"""Create person_budget_configs — the partition-free per-person spend cap.

Issue #4629 (#4620 · C3), design note
`docs/design-notes/4620-cross-org-person-budgets.md` §4.1.

**Why a new table rather than a row in `budget_configs`.** Both existing budget
tables are uniquely keyed `org_id`-first (`src/shared/models/budget.py`), and the
usage tracker writes every entity row — including `root_user` — into the one
`org_id` off the chat log. A person whose runs execute outside the partition
their cap was authored in therefore gets a cap that caps nothing. There is no
place in the current schema for "one ceiling on this person's total agent spend"
because every budget row belongs to a tenant by construction.

`person_budget_configs` carries **no `org_id`**, which is the entire point. The
precedent for a legitimately cross-partition table in this schema is
`tenant_memberships` (migration 021), which is mixin-free for the same reason:
the row is about a person, not about a tenant's data. §4.1 records the three
rejected alternatives (a sentinel `org_id`, the person's home-org partition, and
`parent_tenant_id` fusion) and why each is worse; the model docstring restates
them so nobody "fixes" the table into one.

**Purely additive.** No existing row changes meaning, nothing is backfilled, and
no existing query reads this table until the authoring API in the same PR is
called. That is what makes the rollback in §8.4 trivial: stop reading it, then
drop it. Existing single-org `root_user` caps are deliberately left in place
(§8.1) — moving a cap silently changes what stops a workload.

**No aggregate spend table.** Person-level spend is derived by summing the
existing cross-partition `root_user` rows in `budget_usage`. A second accumulator
would be a denormalised duplicate of the same dollars, which is the #4322
double-count family this repo already carries a migration (032) to clean up
after.

Shape notes, each of which is asserted by
`tests/migrations/test_034_person_budget_configs.py`:

  - **`person_anchor` is `github:<numeric_id>`, not a `users.id`** (§3.3). `users`
    carries `TenantMixin`, so a person onboarded independently into two orgs
    legitimately has two `users.id` values; keying on one of them recreates the
    #4511 inert-cap class one layer up. The GitHub numeric id
    (`user_identities.provider_user_id`) is the same string in every tenant.
  - **`UNIQUE (person_anchor, period_type)`** — one cap per person per period. The
    absence of `org_id` from this key is the feature.
  - **`enforcement_mode` defaults to `soft`** and, in this unit, `soft` is the only
    value the API will write: the person layer is informational here and
    enforcement is #4630 (C4), gated on the §5.7 ruling. The column exists now so
    C4 needs no migration, and so no reader can mistake "informational" for
    "enforcing".
  - **No FK on `authored_by_user_id`.** A platform admin authoring a cap for
    somebody in another tenant is an expected write, and an FK with `ondelete`
    would reintroduce exactly the tenant-lifecycle coupling this table exists to
    avoid. Same reasoning as the missing `org_id`.

An index on `person_anchor` alone is deliberately NOT created: the unique
constraint's index has `person_anchor` as its leading column, so a lookup by
anchor (with or without `period_type`) already seeks it. A second index would be
duplicate write cost for no read.

Revision id is 24 chars, inside the `alembic_version.version_num` VARCHAR(32)
ceiling that `tests/migrations/test_revision_id_length.py` guards (#4123).
Chains onto the real single head, `033_client_tool_capture`.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "034_person_budget_configs"
down_revision: str | Sequence[str] | None = "033_client_tool_capture"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "person_budget_configs"


def upgrade() -> None:
    """Create the partition-free person cap table. See the module docstring."""
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=255), nullable=False),
        # No org_id column, deliberately — see the module docstring.
        sa.Column("person_anchor", sa.String(length=255), nullable=False),
        sa.Column("period_type", sa.String(length=10), nullable=False),
        # NUMERIC(10,2) matches budget_configs.budget_amount_usd exactly. A cap is
        # an authored figure, not an accumulator, so it does not need
        # budget_usage's 6dp (migration 030) — but it must not carry LESS
        # precision than the column clients already render caps from.
        sa.Column("budget_amount_usd", sa.Numeric(precision=10, scale=2), nullable=False),
        # server_default as well as the model default: a row inserted by anything
        # other than the ORM (a migration, an operator's psql session) must not be
        # able to land with a NULL mode, since NULL is neither soft nor hard and a
        # reader would have to guess which.
        sa.Column("enforcement_mode", sa.String(length=10), nullable=False, server_default="soft"),
        sa.Column("authored_by_user_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_anchor", "period_type", name="uq_person_budget_config"),
    )


def downgrade() -> None:
    """Drop the table.

    Trivially reversible precisely because nothing else references it (§8.4): no
    FK points at it, no existing row's meaning depends on it, and person-level
    spend is derived from `budget_usage` rather than accumulated here. The
    operational rollback is still "revert the PR" — this exists so `alembic
    downgrade` walks past the revision cleanly.
    """
    op.drop_table(TABLE)
