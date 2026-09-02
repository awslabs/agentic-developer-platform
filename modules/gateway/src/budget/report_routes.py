"""Mis-partitioned person-cap report — Issue #4627 (C2 of #4620).

``GET /budget/reports/mis-partitioned-caps`` answers the one question the
operator's own $5,000 cap raised and nothing in the platform could answer: *which
``root_user`` caps have I authored in a partition where spend can never accrue?*

**The mechanism, once, because the whole report is one predicate.** Both budget
tables are keyed with ``org_id`` first (``shared/models/budget.py:22``, ``:42``)
and the usage tracker writes every entity row — ``root_user`` included — into the
single ``org_id`` off the chat log. A person whose runs execute outside the
partition their cap was authored in gets a cap that displays, accrues nothing and
stops nothing. Critically the ``entity_id`` is *the same string in both places*
(design note ``4620-cross-org-person-budgets.md`` §2) — only ``org_id`` differs —
which is what makes the cap detectable at all, and is why the report needs no new
storage, no schema change and no backfill.

**Detection, not mutation** (§8.2, and the binding instruction on the issue).
Nothing on this router writes ``budget_configs`` or ``budget_usage``, and no
mutation endpoint is offered even though the fix is mechanical. §8.1 rules that
existing caps stay exactly where they are: moving a cap silently changes what
stops a workload, and a dormant cap is not by itself proof of misconfiguration —
a person may simply not have run yet. The remedy is the operator's two-step
(§8.3); this report is only what tells them which caps need it.

**Why a fourth budget router.** ``src/budget/routes.py`` reads
``entity_type``/``entity_id`` from the request with no scope check at all (open
IDOR #4384) and nothing scope-gated may share its namespace. ``me_routes.py``
structurally accepts no target, which is its guarantee. And this cannot be a
route on ``managed_scope_routes.py`` despite being its nearest neighbour: that
router mounts ``/{entity_type}/{entity_id}`` under ``/budget/scope``, which would
shadow any sibling literal path added after it and answer a valid report request
with a ``422`` for an out-of-allow-list ``entity_type``. Separate prefix, separate
module, no shadowing.

**Authorisation, and why it is org-level and not the managed-scope check.**
#4384's caution applies literally — this surface must not widen. Two properties:

1. **The report accepts NO target parameter of any kind.** The partition comes
   from ``AccessControl.get_user_role``, i.e. the caller's own
   ``tenant_memberships`` row (FR-4.4 of #4401), never from the request. A caller
   cannot name a partition, so there is no id to forge and no IDOR to have.
2. **Org-level authority is required.** The report enumerates every ``root_user``
   principal holding a cap in the partition, which is org-wide scope by
   construction — there is no team or department edge on a ``budget_configs`` row
   to narrow it by. That is exactly why ``managed_scope_routes._check_department_scope``
   already denies a ``dept_admin`` an ``org``-shaped target, and this router
   applies the same rule the only way it can here: by requiring the role itself to
   be org-scoped. A ``dept_admin`` is denied rather than served a partially
   filtered list, because a filter that cannot be expressed must not be
   approximated.

Denial reuses ``managed_scope_routes``' single metadata-free ``403`` verbatim, for
its reason: a denial that varies tells a prober which half of the check they got
right.

**What crosses the tenant boundary: one boolean, and nothing else** (§7.2). A
foreign org's dollar totals are that org's cost data, and disclosing them to an
admin of an unrelated tenant because a person is shared has no membership basis.
So the report carries no foreign ``org_id``, no foreign spend figure and no
foreign run detail — only ``accrues_elsewhere``, which says *whether* a matching
accrual exists in some partition the caller cannot see. That boolean is not a
total, and it is the minimum that distinguishes "authored in the wrong partition"
from "authored correctly and quiet"; without it the report is a list of maybes.

**The cap figure is the RAW authored row, not the platform-clamped effective cap**
— the one deliberate divergence from contract rule 3. Rule 3 exists so a screen
cannot advertise headroom enforcement may not honour; headroom is not this
report's subject, because these caps enforce nothing at all. What the operator
needs is the row *as they typed it*, so they can recognise and delete it (§8.3).
A clamped figure would show a number they never authored.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission, get_admin_config
from src.admin.exceptions import AccessDeniedError, InvalidScopeError
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity.providers import IdentityProvider
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType

from .enforcement_service import _INFRASTRUCTURE_FAULTS
from .me_routes import _principal_kind_for
from .schemas import CAP_PLACES, SERVICE_PRINCIPAL_QUALIFIER, MisPartitionedCapReport, MisPartitionedCapRow, format_money

logger = logging.getLogger("bedrockgateway.budget")

# No `/api` prefix: CloudFront strips the first `/api` segment before the origin
# (#4330, guarded by tests/test_route_prefix_convention.py). The browser calls
# `/api/budget/reports/...`; this router serves `/budget/reports/...`.
#
# `/budget/reports`, deliberately NOT under `/budget/scope`: that prefix's
# `/{entity_type}/{entity_id}` route would shadow a literal sibling path — see the
# module docstring.
router = APIRouter(prefix="/budget/reports", tags=["budget"])


# The roles whose authority is org-wide. See the module docstring for why nothing
# narrower may read this report: a `budget_configs` row carries no team or
# department edge, so a dept-scoped view of it cannot be expressed, and an
# approximation of one is a leak dressed as a filter.
_ORG_SCOPED_ROLES: frozenset[AdminRole] = frozenset({AdminRole.ORG_ADMIN, AdminRole.PLATFORM_ADMIN})

# Verbatim from `managed_scope_routes._DENIAL_DETAIL`. Duplicated rather than
# imported so this router's contract is readable in one file, and pinned equal to
# it by a test — the two must not drift, because a report denial that reads
# differently from a scope denial tells a prober which surface they hit.
_DENIAL_DETAIL = "Not authorized to read budget data for the requested scope."

# One message for every unreadable-store path on this router. A failed read must
# never render as an empty report: "no mis-partitioned caps" and "the check could
# not run" are opposite claims, and conflating them is how an operator concludes
# their caps are fine during an outage.
_UNAVAILABLE_DETAIL = "The mis-partitioned-cap report is temporarily unavailable. This is a backend failure, not a report of zero findings."


def _deny(reason: str, *, caller_org: str) -> HTTPException:
    """Build the single, uniform ``403``.

    The *reason* is logged, never returned: an operator reading logs needs to tell
    "no membership" from "wrong role", while the caller must learn nothing from the
    difference. Returned rather than raised so each call site reads as
    ``raise _deny(...)`` and no path can build a denial without raising it.
    """
    logger.warning("mis_partitioned_cap_report_denied reason=%s caller_org=%s", reason, caller_org)
    return HTTPException(status_code=403, detail=_DENIAL_DETAIL)


async def _authorize_report(access: AccessControl, context: TokenContext) -> str:
    """Resolve the partition to report on, or raise the uniform ``403``.

    The partition is **returned**, never accepted: it comes from the caller's
    ``tenant_memberships`` row via ``get_user_role``, so the route below has no
    parameter a caller could point at another tenant.

    Order matters. The role is resolved first because it supplies the partition
    that ``check_permission`` is then asked about — passing a caller-supplied org
    would make that comparison trivially true, the same reasoning as
    ``managed_scope_routes._resolve_target_org``.

    A ``PLATFORM_ADMIN`` resolves to ``(PLATFORM_ADMIN, None, None)`` from the
    token claim with no membership row, so there is no membership-derived
    partition for them; they report on their **active session tenant**
    (``context.org_id``), which is the same partition every other budget read
    serves them. Deliberately not a parameter: letting a platform admin name an
    arbitrary org would add the cross-tenant target this router exists without,
    and a platform admin who needs another partition switches tenant, exactly as
    they do for ``/me/budget``.

    Returns:
        The ``org_id`` to report on.

    Raises:
        HTTPException: ``403`` for any authorisation failure — including, in
            practice, an authority-store outage: ``get_user_role`` swallows its
            own DB faults and degrades to the fallback role (fail closed), so a
            real outage surfaces as this uniform denial rather than the 503 the
            guard below maps. The guard is kept for any future resolver that
            propagates faults; it must never fall through to allow.
    """
    caller_org = context.org_id

    try:
        role, membership_org, _ = await access.get_user_role(context)
    except _INFRASTRUCTURE_FAULTS as exc:
        # The store that establishes authority is unreadable, so authority cannot
        # be established. 503, never a guess: failing open would serve a partition
        # to a caller whose role was never resolved.
        logger.error("Failed to resolve the caller's role for the mis-partitioned-cap report; refusing the read", exc_info=True)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL) from exc

    if role not in _ORG_SCOPED_ROLES:
        raise _deny(f"role_not_org_scoped:{role.value}", caller_org=caller_org)

    # Belt against the RBAC rollback lever (review fix). Under
    # BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT=false, get_user_role's no-membership
    # fallback grants ORG_ADMIN to principals whose role was never established —
    # an accepted legacy risk for existing surfaces, but this endpoint is a NEW
    # org-wide per-person enumeration and must not inherit it. The route cannot
    # distinguish a row-backed org admin from the fallback (both return the same
    # tuple), so while the lever is in rollback mode the report is platform-admin
    # only: fail closed on a temporary incident lever rather than serve every
    # colleague's cap to an unestablished principal.
    if role is AdminRole.ORG_ADMIN and not get_admin_config().rbac_least_privilege_default:
        raise _deny("org_admin_unverifiable_under_rbac_rollback", caller_org=caller_org)

    target_org = (membership_org or caller_org or "").strip()
    if not target_org:
        # No partition could be established. Denied rather than treated as
        # "all partitions" — an unscoped read of a cross-tenant table is the one
        # outcome this router must not have. Reachable for a platform admin whose
        # token carries no org, which is a real shape.
        raise _deny("no_resolvable_partition", caller_org=caller_org)

    try:
        await access.check_permission(context, Permission.BUDGET_READ, target_org_id=target_org)
    except (AccessDeniedError, InvalidScopeError) as exc:
        # Replaced with the uniform denial rather than propagated: their details
        # name roles, permissions and scopes, which distinguishes "wrong role" from
        # "wrong org" on the wire.
        raise _deny(f"permission_denied:{type(exc).__name__}", caller_org=caller_org) from exc

    return target_org


async def _read_dormant_root_user_caps(db: AsyncSession, org_id: str) -> list[BudgetConfig]:
    """Read the ``root_user`` caps in this partition that no accrual has matched.

    Design note §8.2's predicate, unchanged: ``entity_type='root_user'`` and
    ``NOT EXISTS`` a ``budget_usage`` row agreeing on
    ``(org_id, entity_type, entity_id, period_type)``.

    **There is deliberately no ``period_start`` filter**, matching the note's SQL.
    The signature being detected is "this cap has *never* accrued in its own
    partition", which is a property of the cap's whole lifetime. Adding the current
    period would report every cap whose owner happened not to run this month —
    turning a precise signal into noise an operator learns to ignore, and the note
    is explicit that a dormant cap is a weaker claim than a mis-partitioned one.

    ``service:``-qualified caps are **not** excluded. An unattended trigger's cap
    can be mis-partitioned the same way a person's can, the note's predicate
    excludes nothing, and each row carries ``principal_kind`` so the operator can
    tell the two apart rather than having the choice made for them.
    """
    accrual_exists = (
        select(BudgetUsage.id)
        .where(
            and_(
                BudgetUsage.org_id == BudgetConfig.org_id,
                BudgetUsage.entity_type == BudgetConfig.entity_type,
                BudgetUsage.entity_id == BudgetConfig.entity_id,
                BudgetUsage.period_type == BudgetConfig.period_type,
            )
        )
        .exists()
    )

    stmt = (
        select(BudgetConfig)
        .where(
            BudgetConfig.org_id == org_id,
            BudgetConfig.entity_type == EntityType.ROOT_USER.value,
            ~accrual_exists,
        )
        .order_by(BudgetConfig.entity_id, BudgetConfig.period_type)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _person_ledger_keys(db: AsyncSession, org_id: str, entity_ids: list[str]) -> dict[str, set[str]]:
    """Map each cap's ``entity_id`` to **every** ledger key the same person uses.

    §3.3 and §11 caveat 3 of the note, and the reason this function exists at all:
    a person is *normally* one ``users`` row, so the cap's key and the foreign
    partition's accrual key are the same string. But ``users`` carries
    ``TenantMixin``, and someone independently onboarded into two orgs **can** have
    two ``users`` rows and therefore two ``root_user`` keys (see
    ``tests/shared/test_resolve_root_user_entity_id.py``, where one GitHub account
    has distinct ids per org). Comparing ``entity_id`` alone would silently miss
    exactly the multi-org population this report is for.

    The join key is the **GitHub numeric id** (``user_identities.provider_user_id``),
    the note's person anchor. Two hops:

    1. In-partition: the cap's ``users.id`` -> its GitHub anchor. Scoped to
       ``org_id``, because the anchor belongs to the row that authored the cap.
    2. Cross-partition: that anchor -> every ``users.id`` linked to it in **any**
       tenant. This hop is deliberately unscoped; it is the only cross-partition
       read on this router, it returns identifiers rather than figures, and it is
       what the note's aggregate model is built on (§7.3).

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


async def _keys_accruing_elsewhere(db: AsyncSession, org_id: str, candidate_keys: set[str]) -> set[str]:
    """Which of these ledger keys have a settled ``root_user`` accrual OUTSIDE ``org_id``?

    One batched existence query for the whole report, and it returns **keys only**
    — never the foreign ``org_id`` and never the foreign figure. That is the §7.2
    boundary expressed in the return type: the caller of this function has nothing
    to leak because it was never given anything to leak.

    No ``period_type`` and no ``period_start`` filter, on purpose. The question is
    "does this person's spend land in a different partition *at all*", which is
    what makes the cap unmatched-by-construction rather than unmatched-this-month.
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


async def _display_names(db: AsyncSession, org_id: str, entity_ids: list[str]) -> dict[str, str]:
    """Name the caps' principals from ``users``, org-scoped.

    Same rule and same scoping as ``AdminService._resolve_root_user_display_names``:
    ``root_user`` rows are keyed by canonical ``users.id``, the lookup is confined
    to the reported partition so a row can never be labelled with another tenant's
    person, and ``service:``-qualified ids are simply absent (they have no ``users``
    row by design, and the raw id is the honest label for an automation).
    """
    resolvable = [entity_id for entity_id in entity_ids if not entity_id.startswith(SERVICE_PRINCIPAL_QUALIFIER)]
    if not resolvable:
        return {}

    rows = await db.execute(select(User).where(User.org_id == org_id, User.id.in_(resolvable)))
    return {user.id: (user.name or user.email or user.id) for user in rows.scalars().all()}


@router.get("/mis-partitioned-caps", response_model=MisPartitionedCapReport)
async def get_mis_partitioned_cap_report(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MisPartitionedCapReport:
    """Report the ``root_user`` caps in the caller's partition that can never match.

    Design note ``4620-cross-org-person-budgets.md`` §8.2. Read-only and
    detection-only: nothing here moves, rewrites or deletes a cap (§8.1).

    **No parameters, by design.** The partition is resolved server-side from
    ``tenant_memberships``, so there is no target to forge; org-level authority is
    required because the report's scope is the whole partition (see the module
    docstring). Rows are ordered with the confirmed cross-partition cases first.

    Returns:
        ``200`` with the report. An empty ``rows`` means every ``root_user`` cap in
        this partition has accrued at least once — it never means the check could
        not run.

    Raises:
        HTTPException:
            ``401`` when unauthenticated (from ``get_current_user``);
            ``403`` for any authorisation failure — uniform, metadata-free;
            ``503`` when the ledger or the authority store is unreadable —
            **never** a ``200`` with an empty list, which would read as "none of
            your caps are mis-partitioned".
    """
    access = AccessControl(db)
    org_id = await _authorize_report(access, current_user)

    try:
        caps = await _read_dormant_root_user_caps(db, org_id)
        entity_ids = sorted({cap.entity_id for cap in caps})
        person_keys = await _person_ledger_keys(db, org_id, entity_ids)
        elsewhere = await _keys_accruing_elsewhere(db, org_id, {key for keys in person_keys.values() for key in keys})
        names = await _display_names(db, org_id, entity_ids)
    except _INFRASTRUCTURE_FAULTS as exc:
        logger.error("Failed to build the mis-partitioned-cap report; returning 503 rather than an empty report", exc_info=True)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL) from exc

    rows = [
        MisPartitionedCapRow(
            org_id=cap.org_id,
            entity_id=cap.entity_id,
            display_name=names.get(cap.entity_id),
            principal_kind=_principal_kind_for(EntityType.ROOT_USER, cap.entity_id),
            period_type=cap.period_type,
            # The RAW authored amount, not the platform-clamped effective cap — see
            # the module docstring. The operator has to recognise the row they wrote.
            cap_usd=format_money(cap.budget_amount_usd, CAP_PLACES),
            enforcement_mode=cap.enforcement_mode,
            # Never for `service:` keys (review fix): a service key is a resource
            # name any admin can type, with no per-person identity anchor behind
            # it — so a cross-tenant existence bit on it would let org A author a
            # cap on org B's automation string and read back "spends somewhere",
            # a one-bit probe across the §7.2 boundary. Service caps still list
            # as dormant; they just carry no cross-tenant claim.
            accrues_elsewhere=(not cap.entity_id.startswith(SERVICE_PRINCIPAL_QUALIFIER)) and bool(person_keys.get(cap.entity_id, set()) & elsewhere),
        )
        for cap in caps
    ]

    # Confirmed defects first, then the note's stable secondary order. `sorted` is
    # stable, so the `entity_id`/`period_type` ordering the query already applied
    # survives within each group.
    rows.sort(key=lambda row: not row.accrues_elsewhere)

    confirmed = sum(1 for row in rows if row.accrues_elsewhere)
    logger.info(
        "mis_partitioned_cap_report org=%s rows=%d accrues_elsewhere=%d",
        org_id,
        len(rows),
        confirmed,
    )

    return MisPartitionedCapReport(
        org_id=org_id,
        rows=rows,
        total_row_count=len(rows),
        accrues_elsewhere_count=confirmed,
    )
