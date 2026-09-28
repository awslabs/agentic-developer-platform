"""Verify no stored person-cap anchor is re-keyed by the namespace registry.

Issue #4843 (T4, EPIC #4839). Design note
`docs/design-notes/4828-platform-native-org-team-user.md` §4, §1.6.

What this migration is for
--------------------------

`person_budget_configs.person_anchor` is the ONE place an anchor string is stored
durably (§1.6, verified: spend history in `budget_usage` is keyed by
`users.id`/Cognito `sub`, never by the anchor, so history needs no migration at
all). Introducing a namespace registry with a precedence order is therefore a
data-compatibility question about exactly this column: if precedence changed which
namespace a person resolves to, their stored cap row would still exist, still
display a number, and never match again — an inert cap (#4511) for every affected
person at once.

Because `github:` keeps **priority 1**, the set of rows needing a re-key is
expected to be **empty**. This migration **asserts that emptiness instead of
assuming it** — that is its whole job, and the reason it exists as a migration
rather than as a comment saying "no data change needed".

Two checks, both fail-loud
--------------------------

1. **Every stored anchor is in a REGISTERED namespace.** A row whose namespace the
   registry does not know is a cap nothing will ever resolve — silently unenforced
   today and unfixable later once nobody remembers it was there. Raising here
   surfaces it at deploy time, when someone is watching, rather than leaving it to
   be discovered by a person who overspent.

2. **No `github:`-anchored person would now resolve to a different namespace.**
   This is the precedence-regression guard made executable. It re-runs the
   registry's precedence over the live `user_identities` rows for exactly the
   people who have a stored cap, and raises if any of them would now key
   differently. If a future change reorders the precedence tuple without writing
   the accompanying data migration, THIS migration fails on the deploy that ships
   it — a failed migration (fully rolled back under transactional DDL, nothing
   half-applied) instead of a silent fleet-wide cap outage.

Failing the deploy is deliberately the louder option. The alternative — re-keying
rows automatically — would mean a migration silently rewriting which human a
spending limit governs, based on a precedence order the operator may have changed
by accident.

There is NO `UPDATE` here, and that absence is the contract: on any database
consistent with this release the correct action is provably nothing.

Down-migration
--------------

An intentional no-op, and this is the honest shape rather than a stub: `upgrade()`
writes nothing, so there is nothing to reverse. Downgrading past this revision is
purely a version-pointer move. (If a FUTURE release does reorder precedence and
re-keys rows, that migration owns the inverse UPDATE — it must not be retrofitted
here, where it would run against databases that were never re-keyed.)

`alembic/**` is outside `gateway-ci.yml`'s trigger paths, so a migration-only
change gets zero CI signal; `tests/migrations/test_043_person_anchor_namespace_rekey.py`
is what makes CI run for this at all.

Revision ID: 043_person_anchor_rekey
Revises: 042_user_identity_primary
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "043_person_anchor_rekey"
down_revision: str | None = "042_user_identity_primary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAP_TABLE = "person_budget_configs"
IDENTITY_TABLE = "user_identities"

# The registry as of THIS revision, in precedence order, mirroring
# `src.shared.identity.person_anchor.PERSON_ANCHOR_PROVIDER_PRECEDENCE`. Spelled
# out rather than imported for 009/041's reason: a migration is a snapshot, and
# importing the live registry would make an already-applied migration's meaning
# change whenever the registry does — which would defeat the point of a guard that
# is supposed to fail when the registry moves. Parity with the live tuple is
# asserted by this migration's test.
PROVIDER_PRECEDENCE = ("github", "directory")
INTERNAL_NAMESPACE = "users"
REGISTERED_NAMESPACES = PROVIDER_PRECEDENCE + (INTERNAL_NAMESPACE,)


def _stored_anchors(bind) -> list[str]:
    """Every distinct anchor string in the cap table.

    Returns an empty list when the table does not exist: a database that has not
    run 034 has no caps to check, and this migration must not fail on it.
    """
    if not sa.inspect(bind).has_table(CAP_TABLE):
        return []
    return list(bind.execute(sa.text(f"SELECT DISTINCT person_anchor FROM {CAP_TABLE}")).scalars())


def _resolve_namespace(bind, user_id: str) -> str | None:
    """The namespace this person's anchor resolves to under the CURRENT precedence.

    Mirrors `resolve_person_anchor_identity`'s walk: first provider in precedence
    order for which the person holds an identity row. `None` when they hold none
    (their anchor would be the internal `users:` fallback).
    """
    for provider in PROVIDER_PRECEDENCE:
        exists = bind.execute(
            sa.text(f"SELECT 1 FROM {IDENTITY_TABLE} WHERE user_id = :user_id AND provider = :provider LIMIT 1"),
            {"user_id": user_id, "provider": provider},
        ).scalar()
        if exists:
            return provider
    return None


def upgrade() -> None:
    bind = op.get_bind()
    anchors = _stored_anchors(bind)
    if not anchors:
        return

    # Check 1 — every stored anchor is in a registered namespace.
    unregistered = sorted({a.split(":", 1)[0] for a in anchors if a.split(":", 1)[0] not in REGISTERED_NAMESPACES})
    if unregistered:
        raise RuntimeError(
            f"{CAP_TABLE}.person_anchor holds rows in unregistered namespace(s) {unregistered}. "
            f"Registered namespaces are {list(REGISTERED_NAMESPACES)}. These caps can never be resolved by the "
            f"enforcement layer, so they display a limit and govern nothing (#4511). Resolve them deliberately "
            f"— re-key or delete the rows — then re-run this migration."
        )

    if not sa.inspect(bind).has_table(IDENTITY_TABLE):
        # No identity table means no person can be resolved at all, so precedence
        # cannot have moved anybody. Nothing to check.
        return

    # Check 2 — no stored anchor's owner would now resolve to a DIFFERENT namespace.
    re_keyed: list[str] = []
    for anchor in anchors:
        namespace, _, identifier = anchor.partition(":")
        if namespace not in PROVIDER_PRECEDENCE:
            # An internal `users:` anchor names no identity row to re-resolve. It
            # is also not authorable through the API, so its presence is a
            # pre-existing condition this guard does not adjudicate.
            continue

        # Whom does this anchor belong to? Every `users.id` sharing this
        # provider identity — the same fusion the read path performs.
        owners = list(
            bind.execute(
                sa.text(
                    f"SELECT DISTINCT user_id FROM {IDENTITY_TABLE} WHERE provider = :provider AND provider_user_id = :pid"
                ),
                {"provider": namespace, "pid": identifier},
            ).scalars()
        )
        # No owner: the cap is already orphaned (the person's identity row was
        # removed). Pre-existing, not caused by precedence, and not this
        # migration's business — deleting somebody's cap row is not a decision a
        # schema migration gets to make.
        for user_id in owners:
            if _resolve_namespace(bind, user_id) != namespace:
                re_keyed.append(anchor)
                break

    if re_keyed:
        raise RuntimeError(
            f"The anchor namespace precedence {list(PROVIDER_PRECEDENCE)} would re-key "
            f"{len(re_keyed)} stored cap anchor(s): {sorted(set(re_keyed))[:10]}. Every one of those caps would "
            f"stop enforcing while still displaying a limit (#4511). `github:` must keep priority 1 (design note "
            f"§4, ruling R7); if a reorder is genuinely intended, it needs its own migration that re-keys "
            f"{CAP_TABLE}.person_anchor and a documented inverse."
        )


def downgrade() -> None:
    """No-op by construction — `upgrade()` writes no data. See the module docstring."""
