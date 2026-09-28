"""Make the person-anchor row choice a DB invariant: user_identities.is_primary.

Issue #4843 (T4, EPIC #4839). Design note
`docs/design-notes/4828-platform-native-org-team-user.md` §4 defect (b), and the
blast-radius row "Person-anchor regression".

The defect
----------

`user_identities` has no unique constraint on `(user_id, provider)`. The person
anchor — the cross-org key a spending cap is stored against — is one row's
`provider_user_id`, so when a person holds two rows in one provider, *which* row
is picked decides the key. Today that choice is held by an `ORDER BY
provider_user_id ASC` convention hand-copied into three call sites
(`person_ledger.resolve_person_anchor_identity`,
`person_anchor.resolve_caller_person_anchor`, and the enforcement layer through
the first). If any copy drifts, the authoring side stores a cap under `github:A`
while enforcement looks it up as `github:B`: a row that displays a limit and stops
nothing — the #4511 inert-cap class. That is a convention where an invariant
belongs, and adding a second provider namespace multiplies the surface.

Why a PARTIAL unique index and not `UNIQUE (user_id, provider)`
--------------------------------------------------------------

The plain constraint is what §4 recommends first, and it is **not safe here**;
this was investigated before choosing, per the issue. A same-provider second
account is a legitimate, currently-supported state:

  - `admin/identity/identities_service.add_identity` — the documented
    "admin-linked second GitHub account" path. No guard, no upsert, and no
    IntegrityError handler, so a unique violation surfaces as a raw 500.
  - `admin/identity/users_service.create_user` — its `identities` list accepts two
    entries with the same provider in one request. Same, no handler.
  - `auth/vault_routes` magic-link confirm — links whatever provider the token
    carries. Has a 409 handler, but for the `(provider, provider_user_id, org_id)`
    index, not this one.

Three read paths were also deliberately engineered around the non-uniqueness
(`person_ledger`, `person_anchor`, and `admin/service.py`'s correlated subquery
for `github_username`, which avoids a JOIN specifically because "one user can
carry two GitHub rows"), and two existing tests assert the two-row state is valid.
A plain unique constraint would break all of that at once to fix an anchor-choice
problem.

So the invariant is the narrower true one: **at most one PRIMARY row per
(user_id, provider)**. Multi-account keeps working; the anchor's identity stops
depending on a sort order replicated across files.

Why the backfill is `provider_user_id ASC` and not "newest" or "oldest"
----------------------------------------------------------------------

Because it must be a **no-op in effect**. `person_budget_configs.person_anchor`
stores anchor strings durably, so if the backfill flagged a different row than the
resolvers were already picking, every affected person's live cap would be
instantly orphaned — the exact regression this story exists to avoid. The
resolvers picked the lowest `provider_user_id`; the backfill flags the lowest
`provider_user_id`; the resolvers now order `is_primary DESC, provider_user_id
ASC`. Every anchor is byte-identical across this migration by construction, which
is why migration 043 can assert its re-key set is empty. Among rows that TIE on
the lowest `provider_user_id` — an identical (user_id, provider,
provider_user_id) is storable across orgs, since uniqueness is `(provider,
provider_user_id, org_id)` — exactly one row is flagged (lowest `id`), because
flagging both would violate the partial unique index and abort the migration;
the tied rows carry the same anchor identifier, so the pick cannot re-key
anyone. (Note `admin/service.py`
orders by `created_at` for the username *display* — a pre-existing divergence,
display-only, not an anchor path, and deliberately left alone rather than widening
this change.)

`server_default=false` and NOT NULL together: gateway pods running the pre-042
image INSERT without this column, and a NOT NULL column with no default would fail
every one of those in-flight writes — a routine deploy becoming an identity-link
outage (the 039 reasoning). New rows default to False rather than True so a newly
linked second account cannot silently displace an existing primary and re-key a
live cap; promoting a row is a deliberate act.

The partial index is Postgres-only (SQLite before 3.8 has no partial indexes, and
more to the point `tests/` runs against SQLite where the ORM enforces nothing of
the sort). The COLUMN is created on both, so model/migration parity holds and the
resolvers' `ORDER BY is_primary DESC` works in tests.

Revision ID: 042_user_identity_primary
Revises: 041_directory_provider
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "042_user_identity_primary"
down_revision: str | None = "041_directory_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "user_identities"
COLUMN = "is_primary"
PARTIAL_UNIQUE_INDEX = "uq_user_identities_primary_per_provider"

# Flag exactly the row each anchor resolver was ALREADY picking: the lowest
# `provider_user_id` per (user_id, provider). Correlated subquery rather than a
# window function so the statement runs on both dialects unchanged.
#
# The second predicate is a tie-break, not a choice: uniqueness on this table is
# `(provider, provider_user_id, org_id)`, so two rows with an identical
# (user_id, provider, provider_user_id) in different orgs are a storable state.
# Both tie on MIN(provider_user_id); without the tie-break both would be flagged
# and the partial unique index below would abort the migration mid-deploy on
# data it didn't cause. Which of the tied rows wins is anchor-irrelevant — their
# `provider_user_id` (the anchor identifier) is byte-identical — so MIN(id) is
# picked purely for determinism.
_BACKFILL = f"""
    UPDATE {TABLE}
       SET {COLUMN} = true
     WHERE provider_user_id = (
             SELECT MIN(inner_rows.provider_user_id)
               FROM {TABLE} AS inner_rows
              WHERE inner_rows.user_id = {TABLE}.user_id
                AND inner_rows.provider = {TABLE}.provider
           )
       AND id = (
             SELECT MIN(tied_rows.id)
               FROM {TABLE} AS tied_rows
              WHERE tied_rows.user_id = {TABLE}.user_id
                AND tied_rows.provider = {TABLE}.provider
                AND tied_rows.provider_user_id = {TABLE}.provider_user_id
           )
"""


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column(
        TABLE,
        sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Backfill BEFORE the index exists: the backfill is what makes the invariant
    # true, and creating a unique index over data that violates it fails.
    op.execute(sa.text(_BACKFILL))

    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                f"CREATE UNIQUE INDEX {PARTIAL_UNIQUE_INDEX} "
                f"ON {TABLE} (user_id, provider) WHERE {COLUMN}"
            )
        )


def downgrade() -> None:
    """Drop the index and the column.

    Safe and complete: nothing reads `is_primary` except the anchor resolvers'
    `ORDER BY`, whose `provider_user_id ASC` tiebreaker alone reproduces the
    pre-042 pick exactly. So a rollback returns the anchor to the convention it
    was held by before, with no cap re-keyed.
    """
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(f"DROP INDEX IF EXISTS {PARTIAL_UNIQUE_INDEX}"))
    op.drop_column(TABLE, COLUMN)
