"""Merge budget_usage entity_type 'organization' rows into 'org'.

Issue #4322. A **data-only** migration — no schema change. `entity_type` is
`String(20)` with only a `UniqueConstraint` and no CHECK (001_initial_schema),
which is precisely why the table silently accepted two spellings of one concept
for as long as it did.

Why these rows exist at all: the budget-usage tracker Lambda wrote the
hand-written literal `"organization"` into `budget_usage.entity_type` from #234
until #4322, while budget enforcement has always queried
`EntityType.ORGANIZATION.value` == `"org"` (`src/shared/schemas/budget.py`).
`_check_entity_budget` therefore found no row and read the org's accumulated
spend as `Decimal("0")` on every single request, so the org cap only ever tripped
via the short-TTL Redis reservation from #4287 — never against the persisted
period total. The writer fix lands in the same PR as this migration; this half
makes the ALREADY-RECORDED spend visible to the reader, so the cap starts from
the true period total rather than from zero on the day it deploys.

**Both spellings really do coexist**, so the merge branch below is not
defensive-programming-for-a-hypothetical:

  * `"organization"` — the tracker Lambda, every row it wrote pre-#4322.
  * `"org"` — `src/budget/service.py:376` and
    `src/budget/enforcement_service.py:1229`, which both construct
    `BudgetUsage(entity_type=entity_type.value, ...)`. Any period where an
    operator recorded usage through those paths already has an `"org"` row
    sitting beside the Lambda's `"organization"` one.

So a naive `UPDATE ... SET entity_type = 'org'` is WRONG twice over: it violates
`uq_budget_usage` on every colliding key (aborting the migration), and in the
absence of the constraint it would leave two rows the reader sums as one — the
double-count the issue's impact table calls out, where an org cap fires at half
its true headroom and throttles legitimate work platform-wide.

The three statements below make that outcome impossible:

  1. **Merge into the survivor.** For each `"organization"` row whose
     `(org_id, entity_id, period_start, period_type)` key already has an `"org"`
     row, add its `total_cost_usd` / `total_tokens` / `request_count` onto that
     `"org"` row. Aggregated with `SUM`, not applied per-row, because a single
     key can have only one row of each spelling but the arithmetic must be
     correct regardless.
  2. **Rename the non-colliding remainder.** Where no `"org"` row exists, flip
     `entity_type` in place. Cheaper than insert+delete and it preserves the
     row's `id`, so anything holding a `budget_usage.id` keeps resolving.
  3. **Delete the merged sources.** Only rows still spelled `"organization"`
     remain at this point, and every one of them has had its value folded into a
     surviving `"org"` row by step 1.

**Idempotent by construction.** Every statement is scoped to
`entity_type = 'organization'`, and step 3 removes all such rows, so a second
`upgrade()` matches nothing and changes nothing. That matters more than usual
here: `run-gateway-migrations.yml` is operator-dispatched and a re-run must not
double the ledger it just corrected. Asserted by
`tests/migrations/test_032_budget_usage_org_entity_type.py`.

**What is actually load-bearing here** (verified by mutating each part and
watching the migration tests catch it, rather than asserted from intuition):

  * **Step 2's `NOT EXISTS` guard.** This is the whole correctness argument. It
    partitions the stale rows into "has an `"org"` twin" (handled by step 1) and
    "does not" (renamed here), so the two never touch the same row. Drop the
    guard and the rename hits a colliding key: `uq_budget_usage` aborts the
    migration on Postgres, and the merged total is lost.
  * **Step 3 runs last.** It is the delete; anything before it still needs its
    source rows.
  * **Steps 1 and 2 are order-independent** — the guard already makes them
    disjoint, so a renamed row can never become its own merge source. Stated
    explicitly because the reflex is to assume the sequence matters and then
    "preserve" it with a comment nobody can check.

Alembic wraps all three in one transaction, so a failure at any step leaves the
ledger untouched rather than half-merged.

Rows for `user` / `team` / `agent` / `root_user` / `service_account` /
`department` are matched by none of the three statements and are byte-identical
afterwards. Their writer literals already agree with the reader's enum values;
this migration is deliberately narrow to the one that did not.

`src/ratelimit/models.py` has a SEPARATE `EntityType` whose `ORGANIZATION` is
still `"organization"`. It keys `rate_limit_configs`, not this table, and is
correctly untouched here.

Revision id is 25 chars, inside the `alembic_version.version_num` VARCHAR(32)
ceiling that `tests/migrations/test_revision_id_length.py` guards (#4123).
Chains onto the real single head, `031_usage_graph_address`.
"""

import sqlalchemy as sa

from alembic import op

revision: str = "032_budget_usage_org_type"
down_revision: str | None = "031_usage_graph_address"
branch_labels: str | None = None
depends_on: str | None = None


# The two spellings. `_READER` is `EntityType.ORGANIZATION.value` in
# src/shared/schemas/budget.py — the value budget enforcement queries with.
# `_WRITER` is the literal the tracker Lambda emitted before #4322.
_READER_ENTITY_TYPE = "org"
_WRITER_ENTITY_TYPE = "organization"


def upgrade() -> None:
    """Fold 'organization' rows into their 'org' counterparts. See module docstring."""
    bind = op.get_bind()

    # -- 1. Merge into an existing "org" row on the same key --
    #
    # Correlated subqueries rather than UPDATE...FROM / UPDATE...JOIN: neither of
    # those spellings is portable between Postgres (where this runs) and SQLite
    # (where it is tested), and a migration that cannot be tested on the CI
    # database is a migration nobody has run before production.
    #
    # COALESCE guards the no-match case: without it a key with no
    # "organization" counterpart would have its totals set to NULL, and
    # total_cost_usd is NOT NULL — an aborted migration at best, an undefined
    # enforced denominator at worst. The WHERE EXISTS below already restricts to
    # matching keys, so this is belt-and-braces on a NOT NULL column.
    bind.execute(
        sa.text(
            """
            UPDATE budget_usage SET
                total_cost_usd = total_cost_usd + COALESCE((
                    SELECT SUM(src.total_cost_usd) FROM budget_usage src
                    WHERE src.entity_type = :writer
                      AND src.org_id       = budget_usage.org_id
                      AND src.entity_id    = budget_usage.entity_id
                      AND src.period_start = budget_usage.period_start
                      AND src.period_type  = budget_usage.period_type
                ), 0),
                total_tokens = total_tokens + COALESCE((
                    SELECT SUM(src.total_tokens) FROM budget_usage src
                    WHERE src.entity_type = :writer
                      AND src.org_id       = budget_usage.org_id
                      AND src.entity_id    = budget_usage.entity_id
                      AND src.period_start = budget_usage.period_start
                      AND src.period_type  = budget_usage.period_type
                ), 0),
                request_count = request_count + COALESCE((
                    SELECT SUM(src.request_count) FROM budget_usage src
                    WHERE src.entity_type = :writer
                      AND src.org_id       = budget_usage.org_id
                      AND src.entity_id    = budget_usage.entity_id
                      AND src.period_start = budget_usage.period_start
                      AND src.period_type  = budget_usage.period_type
                ), 0)
            WHERE budget_usage.entity_type = :reader
              AND EXISTS (
                    SELECT 1 FROM budget_usage src
                    WHERE src.entity_type = :writer
                      AND src.org_id       = budget_usage.org_id
                      AND src.entity_id    = budget_usage.entity_id
                      AND src.period_start = budget_usage.period_start
                      AND src.period_type  = budget_usage.period_type
              )
            """
        ),
        {"writer": _WRITER_ENTITY_TYPE, "reader": _READER_ENTITY_TYPE},
    )

    # -- 2. Rename the rows that had nothing to merge into --
    #
    # The NOT EXISTS is the correctness argument, not an optimization: it is the
    # exact complement of step 1's EXISTS, so the two statements operate on
    # disjoint row sets and their relative order does not matter. Remove it and
    # this UPDATE collides with a surviving "org" row on uq_budget_usage,
    # aborting the migration.
    bind.execute(
        sa.text(
            """
            UPDATE budget_usage SET entity_type = :reader
            WHERE entity_type = :writer
              AND NOT EXISTS (
                    SELECT 1 FROM budget_usage dst
                    WHERE dst.entity_type = :reader
                      AND dst.org_id       = budget_usage.org_id
                      AND dst.entity_id    = budget_usage.entity_id
                      AND dst.period_start = budget_usage.period_start
                      AND dst.period_type  = budget_usage.period_type
              )
            """
        ),
        {"writer": _WRITER_ENTITY_TYPE, "reader": _READER_ENTITY_TYPE},
    )

    # -- 3. Drop the merged sources --
    #
    # Whatever still carries the old spelling was folded into a surviving "org"
    # row by step 1 (step 2 renamed everything that was not), so deleting here
    # loses no spend. This is also what makes upgrade() idempotent: nothing is
    # left for a second run to match.
    bind.execute(
        sa.text("DELETE FROM budget_usage WHERE entity_type = :writer"),
        {"writer": _WRITER_ENTITY_TYPE},
    )


def downgrade() -> None:
    """No-op. LOSSY and deliberately irreversible — see below.

    Step 1 of `upgrade()` destroys the information a faithful reverse would need:
    once an `"organization"` row's cost has been summed into an `"org"` row,
    nothing records which share of that total came from which spelling. Splitting
    it back would require inventing a split.

    A no-op is also the *correct* rollback for what this data means. The revert
    path in the issue is "revert the writer literal" — new rows then go back to
    `"organization"` and accumulate separately, while the merged `"org"` rows sit
    there holding real, correctly-summed historical spend. The issue states it
    directly: "Backfilled rows are harmless to leave." Reverting them would
    instead re-hide spend from the reader, which is the bug.

    Declared as a real function rather than omitted so `alembic downgrade` walks
    past this revision cleanly instead of erroring.
    """
