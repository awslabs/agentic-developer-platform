"""Where a person's cloud-agent spend actually accrues — Issue #4669.

One question, asked by two surfaces: **does this person's ``root_user`` spend settle
in some partition other than the one we are looking at?** The
mis-partitioned-cap report (``report_routes.py``, #4627) asks it about caps that
already exist; the budget create path (``admin/service.py``) asks it about a cap
somebody is authoring right now, so the answer can be handed back as a warning
before the operator walks away believing the cap will bite.

The two helpers below started life private to ``report_routes.py``. They live here
because the second caller needed them and **copying them was not an option**: the
predicate is the definition of "mis-partitioned", and two definitions of that would
drift into a report and a warning that disagree about the same row — which is a
worse defect than either surface lacking the check.

This module is deliberately a **leaf**:

* It imports no router, no service and no auth. ``src.budget.__init__`` pulls in
  ``routes`` and therefore ``src.auth``, so any module outside this package must
  import from here **function-locally** (the existing ``budget_helper.py``
  precedent, ``src/admin/service.py``). Keeping this file dependency-free is what
  makes that local import cheap rather than a cycle.
* It contains **no mutation and no HTTP**. ``report_routes.py`` is pinned by a test
  to declare no mutating route, and its detection-only guarantee rests partly on
  what these functions can do. They read; they return identifiers.

**The return type is the tenant boundary (§7.2 of design note
``4620-cross-org-person-budgets.md``).** ``keys_accruing_elsewhere`` returns ledger
**keys only** — never the foreign ``org_id``, never the foreign dollar figure. A
caller cannot leak what it was never given, and both callers only ever need the
boolean "somewhere else, yes or no" plus a count of how many somewheres.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.identity.providers import IdentityProvider
from src.shared.models.budget import BudgetUsage
from src.shared.schemas.budget import EntityType

from .schemas import SERVICE_PRINCIPAL_QUALIFIER

logger = logging.getLogger("bedrockgateway.budget")


async def person_ledger_keys(db: AsyncSession, org_id: str, entity_ids: list[str]) -> dict[str, set[str]]:
    """Map each cap's ``entity_id`` to **every** ledger key the same person uses.

    §3.3 and §11 caveat 3 of the note, and the reason this function exists at all:
    a person is *normally* one ``users`` row, so the cap's key and the foreign
    partition's accrual key are the same string. But ``users`` carries
    ``TenantMixin``, and someone independently onboarded into two orgs **can** have
    two ``users`` rows and therefore two ``root_user`` keys (see
    ``tests/shared/test_resolve_root_user_entity_id.py``, where one GitHub account
    has distinct ids per org). Comparing ``entity_id`` alone would silently miss
    exactly the multi-org population this check is for.

    The join key is the **GitHub numeric id** (``user_identities.provider_user_id``),
    the note's person anchor. Two hops:

    1. In-partition: the cap's ``users.id`` -> its GitHub anchor. Scoped to
       ``org_id``, because the anchor belongs to the row that authored the cap.
    2. Cross-partition: that anchor -> every ``users.id`` linked to it in **any**
       tenant. This hop is deliberately unscoped; it returns identifiers rather
       than figures, and it is what the note's aggregate model is built on (§7.3).

    Only ``github`` identities are followed. The note names the GitHub numeric id
    as *the* anchor; widening to every provider would join people through, say, a
    shared Slack workspace id and could fuse two different humans into one row.

    Returns:
        ``{entity_id: {ledger keys for that person}}``, always including the
        ``entity_id`` itself — so a cap with no linked identity still gets the
        single-``users``-row comparison, which is the common case.
    """
    # Imported here, not at module scope: `src.shared.models.vault` imports
    # `src.shared.identity.providers`, and a top-level import makes the models
    # package circular at collection time (the same note as in
    # `shared/identity/resolver._resolve_via_github_identity`).
    from src.shared.models.vault import UserIdentity

    keys: dict[str, set[str]] = {entity_id: {entity_id} for entity_id in entity_ids}

    # A `service:`-qualified principal has no `users` row by design (#4344), so it
    # has no anchor to follow and is compared on its own id alone.
    resolvable = [entity_id for entity_id in entity_ids if not entity_id.startswith(SERVICE_PRINCIPAL_QUALIFIER)]
    if not resolvable:
        return keys

    anchor_rows = (
        await db.execute(
            select(UserIdentity.user_id, UserIdentity.provider_user_id).where(
                UserIdentity.org_id == org_id,
                UserIdentity.provider == IdentityProvider.github,
                UserIdentity.user_id.in_(resolvable),
            )
        )
    ).all()
    if not anchor_rows:
        return keys

    anchors = {provider_user_id for _, provider_user_id in anchor_rows}
    sibling_rows = (
        await db.execute(
            select(UserIdentity.provider_user_id, UserIdentity.user_id).where(
                UserIdentity.provider == IdentityProvider.github,
                UserIdentity.provider_user_id.in_(anchors),
            )
        )
    ).all()

    siblings_by_anchor: dict[str, set[str]] = {}
    for provider_user_id, user_id in sibling_rows:
        siblings_by_anchor.setdefault(provider_user_id, set()).add(user_id)

    for entity_id, provider_user_id in anchor_rows:
        keys[entity_id] |= siblings_by_anchor.get(provider_user_id, set())

    return keys


async def keys_accruing_elsewhere(db: AsyncSession, org_id: str, candidate_keys: set[str]) -> set[str]:
    """Which of these ledger keys have a settled ``root_user`` accrual OUTSIDE ``org_id``?

    One batched existence query for the whole caller, and it returns **keys only**
    — never the foreign ``org_id`` and never the foreign figure. That is the §7.2
    boundary expressed in the return type: the caller of this function has nothing
    to leak because it was never given anything to leak.

    No ``period_type`` and no ``period_start`` filter, on purpose. The question is
    "does this person's spend land in a different partition *at all*", which is
    what makes a cap unmatched-by-construction rather than unmatched-this-month.
    A period filter would report a cap as merely dormant on the strength of one
    quiet window.
    """
    if not candidate_keys:
        return set()

    rows = await db.execute(
        select(BudgetUsage.entity_id)
        .where(
            BudgetUsage.entity_type == EntityType.ROOT_USER.value,
            BudgetUsage.entity_id.in_(candidate_keys),
            BudgetUsage.org_id != org_id,
        )
        .distinct()
    )
    return set(rows.scalars().all())


async def count_foreign_accrual_partitions(db: AsyncSession, org_id: str, entity_id: str) -> int:
    """How many OTHER partitions this person's cloud-agent spend settles in.

    The write-time question (#4669): an operator authoring a ``root_user`` cap in
    the partition they happen to be signed into wants to know, at that moment, that
    the person's runs bill somewhere else — because the cap they just typed will
    never see a dollar of it. A count rather than a boolean only because "in 2 other
    workspaces" is a materially more actionable sentence than "elsewhere".

    A **count of partitions, not the partitions themselves and not their figures**,
    for the §7.2 reason above: the caller is an org admin of *this* tenant, with no
    authority to learn which other tenants a person belongs to.

    ``service:``-qualified ids return ``0`` without querying. A service key is a
    resource name any admin can type, with no per-person identity anchor behind it,
    so a cross-tenant answer on one would let org A probe org B's automation string
    — the same reasoning that keeps ``accrues_elsewhere`` off service rows in the
    report.
    """
    if entity_id.startswith(SERVICE_PRINCIPAL_QUALIFIER):
        return 0

    keys = (await person_ledger_keys(db, org_id, [entity_id])).get(entity_id, {entity_id})
    elsewhere = await keys_accruing_elsewhere(db, org_id, keys)
    if not elsewhere:
        return 0

    # The partition count needs the distinct foreign `org_id`s, which
    # `keys_accruing_elsewhere` deliberately does not return. Counted here and
    # returned as an integer, so no tenant identifier crosses the boundary.
    rows = await db.execute(
        select(BudgetUsage.org_id)
        .where(
            BudgetUsage.entity_type == EntityType.ROOT_USER.value,
            BudgetUsage.entity_id.in_(elsewhere),
            BudgetUsage.org_id != org_id,
        )
        .distinct()
    )
    return len(set(rows.scalars().all()))
