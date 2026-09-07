"""The person ledger: identity fusion and settled reads — one definition, two consumers.

Extracted from ``me_routes.py`` (review fix on #4689). These are the primitives the
#4620 ruling's load-bearing property rests on — *the displayed number IS the
enforced number* — because ``/me/budget`` composes the person envelope from them
and ``_check_person_budget`` builds its denominator from the SAME functions. As
underscore-privates of a FastAPI routes module they carried no stability contract
for their second consumer; here they are the public API of a deliberate LEAF:

* imports models and shared schemas only — no router, no service, no auth — so
  both ``me_routes`` and ``enforcement_service`` can import it at module level
  without touching their existing cycle;
* contains no mutation and no HTTP: these functions read, and return keys and
  ``Decimal`` figures.

Every function keeps its original docstring; nothing here changed behaviour.
"""

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.identity.providers import IdentityProvider
from src.shared.models.budget import BudgetUsage
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.budget import EntityType, PeriodType

logger = logging.getLogger("bedrockgateway.budget")


async def read_settled_spend(
    db: AsyncSession,
    org_id: str,
    entity_type: EntityType,
    entity_id: str,
    period_type: PeriodType,
    period_start: date,
) -> Decimal:
    """Read settled spend with the full 5-filter predicate.

    ``(org_id, entity_type, entity_id, period_type, period_start)`` — byte-for-byte
    the predicate ``_check_entity_budget`` uses (``enforcement_service.py:1053-1064``),
    which is what makes the endpoint's figure equal the one enforcement compares
    against (FR-1.3). It is also the table's unique constraint, so this reads at
    most one row.

    Dropping any single filter changes the meaning: without ``entity_type`` it
    sums a user's direct spend together with their cloud-agent spend and the
    org total, over-reporting several times over and raising a false "over
    budget" alarm (#4328). There is deliberately no ``SUM`` here at all.

    A missing row is a true ``0`` — this period has no settled spend yet. That is
    a measurement, distinct from a failed read, which propagates as an
    infrastructure fault and becomes a ``503``.
    """
    usage = await db.scalar(
        select(BudgetUsage).where(
            and_(
                BudgetUsage.org_id == org_id,
                BudgetUsage.entity_type == entity_type.value,
                BudgetUsage.entity_id == entity_id,
                BudgetUsage.period_type == period_type.value,
                BudgetUsage.period_start == period_start,
            )
        )
    )
    return usage.total_cost_usd if usage else Decimal("0")


async def resolve_person_anchor_id(db: AsyncSession, canonical_user_id: str) -> str | None:
    """The caller's GitHub anchor id, resolved DETERMINISTICALLY.

    ``user_identities`` has no unique constraint on ``(user_id, provider)``, so a
    person can legitimately hold two GitHub rows (admin-linked second account).
    An unordered ``LIMIT 1`` here and an independent unordered pick on the
    authoring side can each choose a DIFFERENT row — a cap stored under
    ``github:A`` that enforcement looks up as ``github:B``: the #4511 inert-cap
    class (review fix on #4661). Every anchor pick — this one, the authoring
    resolver in ``shared/identity/person_anchor.py``, and the enforcement layer —
    must order identically; ``provider_user_id`` ascending is the convention.
    """
    return await db.scalar(
        select(UserIdentity.provider_user_id)
        .where(
            UserIdentity.user_id == canonical_user_id,
            UserIdentity.provider == IdentityProvider.github,
        )
        .order_by(UserIdentity.provider_user_id)
        .limit(1)
    )


async def resolve_person_identity(db: AsyncSession, canonical_user_id: str) -> tuple[str, list[str]]:
    """Fuse the caller's ``users.id`` rows across tenants, and name the anchor (§3.3).

    The cross-org join key is the **GitHub numeric id**
    (``user_identities.provider_user_id``), per the note's anchor recommendation.
    ``users.id`` stays the ledger key; the anchor is what identifies the *person*
    across partitions.

    Why the indirection is required rather than defensive: a person independently
    onboarded into two orgs can have two ``users`` rows and therefore two distinct
    ``root_user`` ledger keys. A cross-org sum over one canonical id would silently
    omit the other partition's dollars — under-reporting for precisely the
    multi-org population #4620 is about, and doing so invisibly, since a missing
    ledger row is indistinguishable from "no spend".

    Returns:
        ``(anchor, person_user_ids)``. When no GitHub identity is linked the anchor
        is ``users:<canonical id>`` and the key list is the single canonical id —
        which is correct, not a degradation: with no anchor there is no evidence of
        a second ``users`` row to fuse, and inventing one would be a guess. The
        caller's own canonical id is always present in the list, so a linked
        identity that fails to resolve can never *shrink* the read below what the
        single-partition path already covers.
    """
    anchor_id = await resolve_person_anchor_id(db, canonical_user_id)
    if not anchor_id:
        return f"users:{canonical_user_id}", [canonical_user_id]

    # No `org_id` filter, deliberately — and this is the one query in the module
    # that is *supposed* to span tenants. `_resolve_via_github_identity` filters by
    # org because it resolves a WRITE key inside one authorized target org; here the
    # question is the opposite one ("which of this person's rows exist anywhere"),
    # and the authorization is applied to the PARTITION set instead
    # (`_resolve_member_partitions`), which is where it belongs: knowing your own
    # `users.id` values discloses nothing, whereas reading a partition does.
    fused = (
        (
            await db.execute(
                select(UserIdentity.user_id).where(
                    UserIdentity.provider == IdentityProvider.github,
                    UserIdentity.provider_user_id == anchor_id,
                )
            )
        )
        .scalars()
        .all()
    )

    # Sorted for a deterministic read order, with the caller's own id unioned in so
    # the list is never smaller than the single-partition path's one key.
    return f"github:{anchor_id}", sorted({canonical_user_id} | set(fused))


async def resolve_person_subs(db: AsyncSession, person_user_ids: list[str]) -> list[str]:
    """Project the fused ``users.id`` list onto the Cognito subs their DIRECT spend uses.

    Issue #4396. The person's two ledgers are keyed in different namespaces —
    ``root_user`` by canonical ``users.id``, ``user`` by Cognito ``sub`` — so reading
    the direct half needs the subs. This is deliberately a **projection of the
    fusion already performed** by ``_resolve_person_identity``, not a second way of
    deciding who "the same person" is. One fusion, two key namespaces: a third
    independent derivation is the #4511 inert-key class, where one surface reads a
    key nothing writes.

    ``users.cognito_sub`` is **nullable** — shadow users (``POST /resolve-user``)
    and invited-but-never-signed-in users have none. Those rows are skipped rather
    than contributing a ``None``/``""`` key: a row keyed on the empty string would
    be a shared bogus ledger line every such user in the tenant collides on. A user
    with no sub has provably never signed in, so they have no direct spend to miss —
    the omission cannot under-report.

    Note there is no ``org_id`` filter and none is needed: the ids are ``users``
    primary keys already resolved from the caller's own identity, so this reads
    nothing the partition set has not already authorised. Sorted for a deterministic
    read order, exactly as the canonical-id list is.

    Returns:
        The distinct Cognito subs for this person, possibly empty when none of
        their ``users`` rows carries one.
    """
    rows = (await db.execute(select(User.cognito_sub).where(User.id.in_(person_user_ids)))).scalars().all()
    return sorted({sub for sub in rows if sub})


async def read_person_partition_spend(
    db: AsyncSession,
    org_id: str,
    person_user_ids: list[str],
    person_subs: list[str],
    period_type: PeriodType,
    period_start: date,
) -> tuple[Decimal, Decimal]:
    """One partition's settled ``(cloud, direct)`` spend for this person (#4396).

    **The single definition of "this person's spend in this partition", shared
    verbatim by the read surface and the enforcement layer.** ``/me/budget`` calls it
    to compose ``person_envelope``; ``_check_person_budget`` calls it to build its
    denominator. That sharing is the requirement, not a convenience: the ruling is
    that *the displayed number IS the enforced number*, and two independent
    summations of "the same" figure is how a dashboard and a 402 come to disagree
    about whether somebody is over their limit.

    Every dollar is still a single 5-filter row read through the unchanged
    ``_read_settled_spend`` — a widened *key set* over an unchanged predicate, never
    a relaxed predicate and never a SQL aggregate (T33 / #4328).

    The two halves are returned **separately rather than pre-summed** so callers can
    report the components (``per_org[]`` renders both, which is what makes the fused
    headline auditable) while the total stays derived in one place.

    Returns:
        ``(cloud, direct)`` — settled ``root_user`` and ``user`` spend for this
        person in this partition. Both are true ``Decimal("0")`` when no row exists:
        a measurement, not a fallback.
    """
    cloud = Decimal("0")
    for entity_id in person_user_ids:
        cloud += await read_settled_spend(db, org_id, EntityType.ROOT_USER, entity_id, period_type, period_start)

    direct = Decimal("0")
    for sub in person_subs:
        direct += await read_settled_spend(db, org_id, EntityType.USER, sub, period_type, period_start)

    return cloud, direct


async def resolve_member_partitions(db: AsyncSession, person_user_ids: list[str], active_org_id: str) -> list[str]:
    """Derive, server-side, which partitions this person's spend may be read from (§7.3).

    ``tenant_memberships`` is the authority: it is deliberately partition-free (no
    ``TenantMixin``, migration 021) and is the established precedent for a
    legitimately cross-tenant table. The fan-out shape mirrors
    ``src/admin/connections/routes.py:296-306``, sanctioned by
    ``docs/design-notes/3074-invisible-tenancy-per-action-resolution.md`` §1.4.

    **Two unions, both required and neither cosmetic:**

    * ``users.org_id`` — the shadow-user gap. Users auto-provisioned via
      ``POST /resolve-user`` have ``users.org_id`` set but **no** membership row
      (``src/internal/provenance_routes.py:140-147``), so a membership-only list
      silently omits partitions where their spend really accrued. Same fallback
      ``provenance_routes.py:180-190`` applies for the same reason.
    * The caller's **active** partition. It is what every figure above describes,
      so a ``per_org`` list that omitted it would disagree with ``lines`` on the
      same screen. A caller always has standing to read their own session's tenant.

    Note ``is_active`` is NOT filtered on: that flag marks which single membership
    is the caller's current workspace, and filtering by it would reduce this to the
    one partition the endpoint already read — the bug being fixed.

    Returns:
        Sorted tenant ids. This list is the entire authorization boundary of the
        cross-org read, which is why it is computed from the caller's resolved
        identity and never from request input.
    """
    membership_rows = (await db.execute(select(TenantMembership.tenant_id).where(TenantMembership.user_id.in_(person_user_ids)))).scalars().all()
    home_rows = (await db.execute(select(User.org_id).where(User.id.in_(person_user_ids)))).scalars().all()

    return sorted({active_org_id} | {row for row in membership_rows if row} | {row for row in home_rows if row})
