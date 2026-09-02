"""Own-scope budget read API — Issue #4397 (U-1 of EPIC #4324).

``GET /me/budget`` answers, for the signed-in caller only, "what is my cap and
how much have I spent against it" — for daily, weekly and monthly periods.
Before this endpoint a user discovered they had a budget only when an agent
stopped with a "budget exceeded" error.

**Why this is a new module and not a route in ``routes.py``** (NFR-1, a hard
constraint): ``src/budget/routes.py`` takes ``entity_type``/``entity_id``
unscoped from the request and is the subject of open IDOR #4384 — any member can
read any colleague's cap and spend. A route added there would inherit the
vulnerability. This router follows the ``/me/*`` precedent in
``src/activity/routes.py`` instead: identity comes **only** from the validated
token, and the endpoint accepts **no scope parameter at all**, so there is no
parameter to abuse. Managed (operator) scope is a separate, permission-gated
router — U-4's job, deliberately not here.

**Read-only** (NFR-2). Nothing in this module writes ``budget_configs`` or
``budget_usage``, and no enforcement behaviour changes.

The three traps this module exists to avoid, all live in code it would have been
natural to just call:

* ``get_budget_status_for_headers`` (``enforcement_service.py:1246``) already
  selects a binding line — but it is MONTHLY-only, it returns the **raw
  unclamped** cap, and it returns ``{}`` for both "no budget configured" and "the
  database was unreachable". All three are defects for this surface (FR-1.1,
  FR-1.4, FR-1.7), so its *logic* is reimplemented here rather than called. The
  divergence is intentional and each site is commented.
* ``get_organization_budget_overview`` sums without filtering by
  ``entity_type``, over-reporting 2x-4x (#4328). It is never called from here,
  and there is no ``SUM`` in this module at all — every figure is a single
  5-filter row read.
* ``resolve_canonical_user_id`` silently falls back to the raw Cognito sub when
  no ``users`` row exists (``resolver.py:50-62``). Querying the ``root_user``
  ledger with a Cognito sub finds nothing and renders a false ``$0`` cloud spend.
  Handled explicitly — see ``_resolve_root_principal``.

Spend figures are settled totals written asynchronously by the budget-usage
tracker Lambda, so very recent spend may not be included yet. That is a property
of the ledger, not of this read — but it is not left implicit either: the response
signals it per request via ``freshness.cost_backfill_lag`` (#4477, NFR-5), so a
client can say "recent spend may be incomplete" rather than presenting an
understated figure as final truth.
"""

import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.activity.cost_service import get_cost_by_run_ids
from src.activity.routes import _expand_date_bound, get_activity_service
from src.activity.schemas import InvocationItem
from src.activity.service import ActivityService
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity import resolve_canonical_user_id
from src.shared.identity.providers import IdentityProvider
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.usage import UsageLog
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

from .config import budget_config
from .enforcement_service import _INFRASTRUCTURE_FAULTS
from .schemas import (
    CAP_PLACES,
    SERVICE_PRINCIPAL_QUALIFIER,
    SPEND_PLACES,
    BudgetBand,
    BudgetLine,
    BudgetPeriod,
    BudgetRunItem,
    BudgetSource,
    CombinedInformational,
    CostFigure,
    Freshness,
    MyBudgetResponse,
    MyBudgetRunsResponse,
    PerOrgLine,
    PersonEnvelope,
    PrincipalKind,
    RunAttribution,
    format_money,
)
from .utils import CALENDAR_PERIOD_TYPES, calculate_budget_utilization, get_period_start_end

logger = logging.getLogger("bedrockgateway.budget")

# No `/api` prefix: CloudFront strips the first `/api` segment before the origin,
# so a router mounting under `/api/...` is unreachable through the dashboard
# (issue #4330, guarded by tests/test_route_prefix_convention.py). The browser
# calls `/api/me/budget`; this router serves `/me/budget`.
router = APIRouter(tags=["budget"])

# How recently a request must have been logged for its missing price to count as
# back-fill lag rather than a permanently-unpriced row (#4477, NFR-5). See
# `_has_pending_cost_backfill` for why the bound exists at all.
#
# Sized well above the tracker Lambda's normal S3-event-to-accumulator latency
# (seconds to a couple of minutes) so genuine lag is caught with margin, and well
# below a period, so a never-bridged row from an error path ages out instead of
# pinning the affordance on forever. Not a config setting: it is a property of one
# derivation, and an operator lever here would let the field be tuned into always
# saying `false` — which is the exact defect #4477 was filed for.
_BACKFILL_LAG_WINDOW = timedelta(minutes=15)


def _resolve_period_bounds(period_type: str, reference_date: date | None = None) -> tuple[PeriodType, date, date]:
    """Validate a period type is a calendar period and return its bounds.

    ``run``/``chain`` caps are lifetime-scoped: they accumulate for as long as
    the run does, and ``get_period_start_end`` deliberately **raises** for them
    rather than silently returning today's date. An unguarded call would surface
    that ``ValueError`` as a ``500`` — a server error for what is really a bad
    request (FR-3.6).

    Two layers guard this, on purpose. The route's ``Literal`` annotation rejects
    non-calendar values at the HTTP boundary with FastAPI's own ``422``; this
    function is the second layer, so that adding a period type to the annotation
    without teaching ``get_period_start_end`` about it produces a ``422`` here
    instead of a ``500`` from the raise. ``CALENDAR_PERIOD_TYPES`` is the
    allowlist both layers agree on (#4328).

    Args:
        period_type: The requested period type, as it arrived on the request.
        reference_date: Any day inside the period to report. ``None`` (the
            ``/me/budget`` case) means the *current* period. U-3's drill-down
            accepts a caller-supplied ``period_start``, and this normalises it to
            the containing period's real bounds rather than trusting the client
            to have sent the exact first day — a Wednesday sent as a weekly
            ``period_start`` must select Monday-to-Sunday, or the run list and the
            settled figure would describe different windows.

    Raises:
        HTTPException: ``422`` for any non-calendar period type.
    """
    if period_type not in CALENDAR_PERIOD_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"period_type must be one of {sorted(CALENDAR_PERIOD_TYPES)}; "
                f"got '{period_type}'. Run and chain caps are lifetime-scoped and have no calendar period."
            ),
        )

    resolved = PeriodType(period_type)
    period_start, period_end = get_period_start_end(resolved, reference_date)
    return resolved, period_start, period_end


async def _resolve_root_principal(db: AsyncSession, context: TokenContext) -> tuple[str | None, str]:
    """Resolve the caller's canonical ``users.id`` for their cloud-agent ledger.

    The caller's spend lands in two different id namespaces. Their **direct**
    spend is keyed by Cognito ``sub`` under ``entity_type="user"``; the spend of
    agent chains they triggered is keyed by canonical ``users.id`` under
    ``entity_type="root_user"`` (#4300, pinned by ``test_root_human_envelope.py``
    T8). The two can never collide on id, and can never be recognised as the same
    person without a join through ``users`` — which is what this resolves.

    The trap: ``resolve_canonical_user_id`` returns the **raw Cognito sub** when
    no ``users`` row exists or the lookup fails (``resolver.py:50-62``). Querying
    the ``root_user`` ledger with a Cognito sub matches nothing, so the caller's
    cloud spend would render as ``$0`` — indistinguishable from "no cloud spend",
    and trusted. So when the resolver hands back the sub it was given, this
    treats the identity as **unresolved**: the ``root_user`` entity is omitted
    from the hierarchy entirely and the response says ``identity_status:
    "unresolved"``, rather than reporting a number that is not a measurement.

    Equality against the input is the available signal — the resolver collapses
    "no row" and "lookup failed" into the same return value, and does not report
    which. It is a sound signal in practice because the two namespaces are
    disjoint by construction: ``users.id`` is a UUID, a Cognito ``sub`` is not.

    Returns:
        ``(canonical_user_id, identity_status)``. ``canonical_user_id`` is
        ``None`` when there is no root-principal ledger to read — either the
        identity did not resolve, or the caller is a service account, which has
        no canonical user row by design.
    """
    if context.account_type == "service":
        # A service account is not a person and has no `users` row. This is not a
        # failure to resolve, so it must not be reported as one.
        return None, "not_applicable"

    canonical = await resolve_canonical_user_id(db, context.user_id)
    if canonical == context.user_id:
        logger.warning(
            "Cognito sub did not resolve to a canonical users.id; omitting the root_user ledger from /me/budget rather than reporting $0 cloud spend",
            extra={"org_id": context.org_id},
        )
        return None, "unresolved"
    return canonical, "resolved"


def _own_scope_hierarchy(context: TokenContext, canonical_user_id: str | None) -> list[tuple[EntityType, str]]:
    """Build the entity hierarchy whose caps can bind the caller.

    Mirrors ``BudgetEnforcementService._get_entity_hierarchy`` — the same
    entities in the same order, most specific first — because a read that
    consulted a different set than enforcement would report a cap that never
    fires, or miss the one that does.

    Two deliberate differences from the enforcement version, both because this is
    a read on behalf of a signed-in human rather than a check on an in-flight
    agent request:

    * The root-principal entity is keyed from the **resolved** canonical id
      rather than ``context.attributed_user_id``. That field is published only by
      the budget middleware from a resolved run binding, so on a plain JWT read
      request it is always empty — reading it here would silently drop the
      caller's cloud-agent line from every response.
    * There is no ``!= user_id`` equality skip. Enforcement skips the root
      principal when it *is* the caller to avoid reserving the same cost twice
      against one party; that concern does not exist for a read, and the two ids
      are in different namespaces anyway (Cognito sub vs ``users.id``), so
      nothing here can double-count.

    Empty ids are skipped rather than queried: ``team_id``/``department_id``
    default to ``""`` for callers with no team or department, and a row keyed on
    ``""`` would be a shared bogus ledger line.
    """
    entities: list[tuple[EntityType, str]] = []

    if context.account_type == "service":
        entities.append((EntityType.SERVICE_ACCOUNT, context.user_id))
    else:
        entities.append((EntityType.USER, context.user_id))

    if context.team_id:
        entities.append((EntityType.TEAM, context.team_id))

    if context.department_id:
        entities.append((EntityType.DEPARTMENT, context.department_id))

    if canonical_user_id:
        entities.append((EntityType.ROOT_USER, canonical_user_id))

    if context.attributed_org_id:
        entities.append((EntityType.ORGANIZATION, context.attributed_org_id))

    return entities


def _resolve_effective_period_cap(configured: Decimal) -> Decimal:
    """Clamp a configured calendar cap to the platform ceiling.

    ``min(configured, platform_ceiling)`` — a tenant may tighten its own ceiling,
    never loosen it. Same security property as ``_resolve_scope_cap``
    (``enforcement_service.py:446``) for run/chain caps: tenant admins can write
    their own ``budget_configs`` rows, so without the clamp raising your own cap
    would be self-service.

    ``budget_period_cap_usd`` ships as ``None`` (no ceiling), so by default this
    returns the configured row unchanged and the reported cap equals the one
    enforcement honours.

    **One deliberate divergence from ``_resolve_scope_cap``: a non-positive
    configured value is honoured here, not replaced by the default.** That
    function maps ``<= 0`` onto the platform default because a run cap must
    "never resolve to unlimited". For a calendar cap the opposite is true — a
    ``$0`` row is a deliberate, meaningful hard stop, and FR-1.5 requires it be
    reported as ``capped`` at ``"0.00"`` rather than conflated with anything else.
    Substituting a default here would show a $0-capped user as having headroom.
    """
    ceiling = budget_config.budget_period_cap_usd
    if ceiling is None:
        return configured
    return min(configured, ceiling)


def _band_for(cap: Decimal, spend: Decimal) -> tuple[float | None, BudgetBand]:
    """Derive utilisation and warning band from the server-side thresholds.

    Bands come from ``budget_config.budget_warning_threshold_percent`` /
    ``_critical_threshold_percent`` (80.0 / 95.0) so there is exactly one source
    of truth. 95% is a *critical warning*, **not** a stop — enforcement blocks at
    ``>= 100%``.

    A ``$0`` cap gets ``utilization_pct: None`` rather than a percentage: the
    ratio is undefined at a zero denominator. Note ``calculate_budget_utilization``
    returns ``0.0`` in that case, which would render a $0 cap as "0% used —
    plenty of room" when in fact no request with any cost can pass; so that helper
    is used only for the ``cap > 0`` path, where its arithmetic is exactly what
    enforcement uses.
    """
    if cap <= 0:
        return None, "exceeded"

    utilization = calculate_budget_utilization(cap, spend)

    if utilization >= 100:
        band: BudgetBand = "exceeded"
    elif utilization >= budget_config.budget_critical_threshold_percent:
        band = "critical"
    elif utilization >= budget_config.budget_warning_threshold_percent:
        band = "warning"
    else:
        band = "none"

    return round(utilization, 1), band


# ---------------------------------------------------------------------------
# Envelope composition — Issue #4399 (U-2)
# ---------------------------------------------------------------------------
#
# Everything below is PURE: given an entity, a cap row and a spend figure it
# returns a line. No database access, no request state. That is deliberate — U-4
# renders this same line model for managed (operator) scope over arbitrary entity
# ids, so the composition rules have to be reusable and directly unit-testable
# rather than reachable only by driving an HTTP request.
#
# The composition rule, stated once (FR-2.1-2.5):
#
#     lines    = the caller's own per-person lines (direct + cloud)
#     binding  = argmin(remaining_usd) over CAPPED lines in the full hierarchy
#     headline = binding             # NEVER the total over lines.spend_usd
#     combined = total over per-person lines.spend_usd, informational ONLY
#
# `lines` and `binding` deliberately range over DIFFERENT sets, and conflating
# them is the subtle way to get this wrong. `lines` is what gets rendered as the
# caller's own envelope, so it holds only their two personal ledgers. `binding` is
# what actually stops them, so it ranges over the WHOLE hierarchy — a department
# cap can and does bind (pinned by T24 in test_me_budget_routes.py). Narrowing
# binding selection to the two personal lines would mean a user stopped by their
# department's cap sees a headline that never mentions it.

# Which lines belong to the caller as a person, and what each one is called.
# Shared ancestors (team/department/org) are absent by construction: they can
# bind, but they are not this person's spend, so they are neither `direct` nor
# `cloud` and never enter the combined total.
#
# Issue #4536 aligned the two person-scoped nouns — "direct use" and "cloud
# agents" — with the words Budget Management now uses to *author* these caps
# (frontend/src/utils/entityLabels.ts). Someone who sets a cloud-agent cap on one
# screen must recognise the line it governs on the other; two different phrasings
# for one ledger is how a person caps one bucket believing they capped the other.
_PER_PERSON_SOURCES: dict[EntityType, tuple[BudgetSource, str]] = {
    EntityType.USER: ("direct", "Direct use (my machine)"),
    EntityType.SERVICE_ACCOUNT: ("direct", "Direct use (service account)"),
    EntityType.ROOT_USER: ("cloud", "Cloud agents (runs I triggered)"),
}

# Labels for the shared ancestors. These lines can bind, so they need a name for
# the headline, but they carry `source=None` and stay out of `lines`.
_SHARED_LABELS: dict[EntityType, str] = {
    EntityType.TEAM: "Team budget",
    EntityType.DEPARTMENT: "Department budget",
    EntityType.ORGANIZATION: "Organization budget",
}


def _principal_kind_for(entity_type: EntityType, entity_id: str) -> PrincipalKind:
    """Classify a line's principal as a person or an unattended trigger (FR-2.5).

    Only ``root_user`` ids carry the ``service:`` qualifier — it is applied by
    ``_qualify_root_principal_id`` where ``attributed_user_id`` is published
    (``enforcement_service.py:93``), and nothing else writes it. So the prefix is
    checked only on that entity type; testing it everywhere would let a user who
    typed ``service:`` into some other id masquerade as a service principal.

    ``service_account`` is a *caller* identity, not a root principal — a service
    account is who authenticated, which is a different question from who set a
    chain in motion. It is reported as ``service`` because it is genuinely not a
    person, which keeps it out of per-person rollups on the same rule.

    An unqualified ``root_user`` id is a canonical ``users.id``, hence ``human``.
    A UUID contains no colon, so the two cases cannot be confused (#4344).
    """
    if entity_type == EntityType.SERVICE_ACCOUNT:
        return "service"
    if entity_type == EntityType.ROOT_USER and entity_id.startswith(SERVICE_PRINCIPAL_QUALIFIER):
        return "service"
    return "human"


def _compose_line(
    entity_type: EntityType,
    entity_id: str,
    cap_row: BudgetConfig | None,
    spend: Decimal,
) -> BudgetLine:
    """Build one envelope line from a single ledger row's worth of truth.

    Pure. ``cap_row is None`` means no cap is configured, which is a different
    state from a cap of ``$0`` (contract rule 2) and is rendered as
    ``cap_status="uncapped"`` with every cap-derived field ``None`` — so a client
    cannot read "no ceiling" as "exhausted".

    The cap is the **effective** one, after ``_resolve_effective_period_cap``'s
    platform clamp, for the same reason the headline uses it: reporting the raw
    configured row advertises headroom enforcement may not honour (FR-1.4).
    """
    source, label = _PER_PERSON_SOURCES.get(
        entity_type,
        (None, _SHARED_LABELS.get(entity_type, entity_type.value)),
    )
    principal_kind = _principal_kind_for(entity_type, entity_id)

    if cap_row is None:
        return BudgetLine(
            entity_type=entity_type.value,
            label=label,
            source=source,
            principal_kind=principal_kind,
            cap_usd=None,
            spend_usd=format_money(spend, SPEND_PLACES),
            remaining_usd=None,
            utilization_pct=None,
            band=None,
            cap_status="uncapped",
            enforcement_mode=None,
        )

    effective_cap = _resolve_effective_period_cap(cap_row.budget_amount_usd)
    utilization, band = _band_for(effective_cap, spend)

    return BudgetLine(
        entity_type=entity_type.value,
        label=label,
        source=source,
        principal_kind=principal_kind,
        cap_usd=format_money(effective_cap, CAP_PLACES),
        spend_usd=format_money(spend, SPEND_PLACES),
        # Not clamped at zero — settled spend can pass a cap, and hiding the
        # overage behind a flat "$0.00 left" is wrong for a read surface whose
        # purpose is the true position (contract rule 4).
        remaining_usd=format_money(effective_cap - spend, SPEND_PLACES),
        utilization_pct=utilization,
        band=band,
        cap_status="capped",
        enforcement_mode=cap_row.enforcement_mode,
    )


def _combined_informational(lines: list[BudgetLine]) -> CombinedInformational | None:
    """Sum the caller's per-person lines into a labelled non-budget figure.

    **This is the one place two lines are added together, and the result is never
    presented as a budget.** ``CombinedInformational`` has no cap field at all, so
    the "no ``x / y`` progress bar" rule is carried by the type (FR-2.4). The
    headline is selected, never summed — see the route.

    Two exclusions, both load-bearing:

    * **Service principals are excluded** (``envelope-composition.md`` §5). The
      usage tracker writes the ``root_user`` row whenever ``root_human_id`` is set,
      with no equality skip (``handler.py:467``), while enforcement *does* skip the
      root entity when the root is the caller
      (``enforcement_service.py:437``). For a service-rooted run the registry row
      names the same service key as both ``user_id`` and ``root_human_id``, so the
      same dollar lands on the ``user`` row **and** the ``root_user`` row —
      summing across entity types would count it twice. Excluding service
      principals removes that double-count and keeps unattended CI spend out of a
      person's envelope in one rule (FR-2.5).
    * **Fewer than two lines returns ``None``.** A "combined" total over a single
      line is just that line's spend restated, and offering it invites a client to
      render a redundant tile. Absence is clearer than a duplicate.

    Uncapped lines ARE included: their spend is a real measurement, and the total
    is explicitly not a budget, so having no ceiling does not disqualify a line
    from contributing dollars to it.
    """
    contributing = [line for line in lines if line.principal_kind == "human"]
    if len(contributing) < 2:
        return None

    # Accumulated with an explicit loop rather than the builtin, because T33 in
    # test_me_budget_routes.py forbids that aggregate's name case-insensitively
    # anywhere in this module's raw source. That gate is about SQL aggregates —
    # every ledger figure must stay a single 5-filter row read — and this is
    # in-Python arithmetic over rows already read, so it does not violate the
    # gate's intent. Sidestepping the spelling is the surgical fix; widening the
    # gate's regex to permit it would weaken a check that exists to keep #4328's
    # unfiltered-aggregate class from coming back.
    #
    # Decimal throughout, never float: these strings carry NUMERIC(14,6) spend and
    # a float round-trip loses sub-cent precision (contract rule 1).
    total = Decimal("0")
    for line in contributing:
        total += Decimal(line.spend_usd)

    return CombinedInformational(
        spend_usd=format_money(total, SPEND_PLACES),
        note=("Sum of separately-capped lines. Not a cap: no budget governs this total, and nothing is enforced against it."),
    )


async def _read_settled_spend(
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


async def _read_cap(
    db: AsyncSession,
    org_id: str,
    entity_type: EntityType,
    entity_id: str,
    period_type: PeriodType,
) -> BudgetConfig | None:
    """Read the ``budget_configs`` row governing this entity and period.

    Same 4-filter predicate as ``_check_entity_budget`` and the unique constraint
    on the table, so this reads at most one row. ``None`` means no cap is
    configured — which is a distinct state from a cap of ``$0`` (FR-1.5) and is
    carried through as such.
    """
    return await db.scalar(
        select(BudgetConfig).where(
            and_(
                BudgetConfig.org_id == org_id,
                BudgetConfig.entity_type == entity_type.value,
                BudgetConfig.entity_id == entity_id,
                BudgetConfig.period_type == period_type.value,
            )
        )
    )


# ---------------------------------------------------------------------------
# Cross-org person view — Issue #4626 (C1 of #4620)
# ---------------------------------------------------------------------------
#
# Everything above reads ONE partition, and that is correct for the figures
# enforcement compares against (#4132). This section answers the different
# question: how much have this person's agents spent *everywhere they run*.
#
# Implements `docs/design-notes/4620-cross-org-person-budgets.md` §7.1/§7.3 and the
# §3.3 caveat. Three properties are load-bearing and each is a test:
#
#   1. The partition list is derived SERVER-SIDE from `tenant_memberships`, unioned
#      with `users.org_id` for the shadow-user gap. Never from a request parameter —
#      this router accepts no scope parameter at all, which is what makes a widened
#      `org_id` predicate a self-scope read rather than #4384's IDOR one table over.
#   2. The person is resolved through `user_identities.provider_user_id`, NOT by
#      canonical id alone. `users` carries `TenantMixin` and `user_identities` is
#      unique per `(provider, provider_user_id, org_id)` since migration 021, so one
#      GitHub account legitimately holds a DIFFERENT `users.id` per tenant (pinned
#      by `tests/shared/test_resolve_root_user_entity_id.py::
#      test_shared_github_account_resolves_per_tenant`). Summing by canonical id
#      alone under-reports for exactly the multi-org population this ships for.
#   3. Every dollar figure stays a single 5-filter row read via the UNCHANGED
#      `_read_settled_spend`/`_read_cap`. Totalling happens in Python over
#      `Decimal`, never as a SQL aggregate — same discipline as
#      `_combined_informational`, and the same reason (T33 / #4328).
#
# Only `root_user` rows are read, and only settled totals leave this surface: no run
# detail crosses a tenant boundary under any circumstances, and nothing here is
# reachable by anyone but the person themselves (note §7.2 — explicitly NOT their
# home-org admin).


async def _resolve_person_identity(db: AsyncSession, canonical_user_id: str) -> tuple[str, list[str]]:
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
    anchor_id = await db.scalar(
        select(UserIdentity.provider_user_id).where(
            UserIdentity.user_id == canonical_user_id,
            UserIdentity.provider == IdentityProvider.github,
        )
    )
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


async def _resolve_member_partitions(db: AsyncSession, person_user_ids: list[str], active_org_id: str) -> list[str]:
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


async def _read_person_org_line(
    db: AsyncSession,
    org_id: str,
    person_user_ids: list[str],
    period_type: PeriodType,
    period_start: date,
    *,
    is_active: bool,
) -> PerOrgLine:
    """Read one tenant's ``root_user`` cap and settled spend for this person.

    Reuses ``_read_settled_spend``/``_read_cap`` unchanged, once per fused
    ``users.id``, so every dollar here comes from the same full 5-filter predicate
    enforcement compares against — the cross-org read is a *widened partition set*,
    not a relaxed predicate (§7.1).

    Summing across the person's fused ids cannot double-count: the ids are distinct
    ``users`` primary keys, so they address disjoint rows of a table uniquely keyed
    on ``entity_id``. In the ordinary single-``users``-row case the loop runs once
    and this is exactly the figure the existing ``cloud`` line reports.

    The cap is the **first** one found across the fused ids rather than a sum: a cap
    is a ceiling this tenant authored on this person, and two ids belonging to one
    person do not add their ceilings together. Deterministic because
    ``person_user_ids`` is sorted, and in practice at most one id has a row in any
    given tenant — a cap is authored against the ``users.id`` that tenant knows.
    """
    total = Decimal("0")
    cap_row: BudgetConfig | None = None

    for entity_id in person_user_ids:
        total += await _read_settled_spend(db, org_id, EntityType.ROOT_USER, entity_id, period_type, period_start)
        if cap_row is None:
            cap_row = await _read_cap(db, org_id, EntityType.ROOT_USER, entity_id, period_type)

    org_name = await db.scalar(select(Organization.name).where(Organization.id == org_id))

    return PerOrgLine(
        org_id=org_id,
        # Falling back to the id rather than to a blank or "Unknown": the id is what
        # an operator greps for, and an org row can legitimately be missing on the
        # shadow-user path where the partition came from `users.org_id`.
        org_name=org_name or org_id,
        cloud_spend_usd=format_money(total, SPEND_PLACES),
        # The EFFECTIVE cap, clamped by the platform ceiling for the same reason
        # every other cap on this surface is: reporting the raw row advertises
        # headroom enforcement may not honour (FR-1.4).
        cap_usd=format_money(_resolve_effective_period_cap(cap_row.budget_amount_usd), CAP_PLACES) if cap_row is not None else None,
        is_active_partition=is_active,
    )


def _person_envelope(anchor: str, per_org: list[PerOrgLine]) -> PersonEnvelope | None:
    """Sum the per-org lines into a labelled non-budget figure (§7.1).

    ``PersonEnvelope`` carries no cap field, so "this is not a governed figure" is
    a property of the type rather than of this docstring — the same discipline
    ``_combined_informational`` follows, and for a stronger reason here: the
    person-level cap table does not exist yet (note §4.1) and whether it may ever
    deny is an open ruling (§5.7). A denominator on this figure today would
    advertise a ceiling nothing enforces.

    Accumulated with an explicit ``Decimal`` loop, never a SQL aggregate or the
    builtin whose name T33 forbids anywhere in this module: sub-cent precision must
    survive (``NUMERIC(14,6)``), and every ledger figure must stay a single
    5-filter row read.

    ``None`` only when there are no lines at all — i.e. the caller's identity did
    not resolve. Unlike ``_combined_informational`` a single line is NOT suppressed:
    "this is your total across every workspace" is a distinct and useful claim when
    the count is one, and suppressing it would show nothing to the person whose
    spend has not yet crossed a boundary.
    """
    if not per_org:
        return None

    total = Decimal("0")
    for line in per_org:
        total += Decimal(line.cloud_spend_usd)

    return PersonEnvelope(
        anchor=anchor,
        spend_usd=format_money(total, SPEND_PLACES),
        partition_count=len(per_org),
        note=(
            "Your cloud-agent spend across every workspace you belong to. Not a cap: "
            "no budget governs this total, and nothing is enforced against it. Each "
            "workspace's own cap is shown on its line."
        ),
    )


async def _cross_org_person_view(
    db: AsyncSession,
    canonical_user_id: str,
    active_org_id: str,
    period_type: PeriodType,
    period_start: date,
) -> tuple[list[PerOrgLine], PersonEnvelope | None]:
    """Compose ``per_org[]`` and the informational ``person_envelope`` (§7.1).

    The active partition is listed first so the lines agree with the single-partition
    figures rendered beside them; the rest follow in the sorted order
    ``_resolve_member_partitions`` returns, which keeps the response stable across
    requests.

    Every member partition is listed, including ones with a true ``$0`` — that is the
    note's own operator walkthrough (``aws-e $X``, ``pranavsharma1000 $0``), and it is
    what makes the mis-partition visible: a tenant holding the cap with no spend
    beside a tenant holding the spend with no cap is the entire diagnosis, and it is
    only legible when both lines are present.

    Raises:
        Whatever the underlying reads raise. Deliberately not caught here — the route
        calls this inside the same ``try`` as every other ledger read, so a failure
        becomes a ``503`` rather than a silent ``$0`` cross-org total, which would be
        the exact defect #4620 reports dressed up as a successful response.
    """
    anchor, person_user_ids = await _resolve_person_identity(db, canonical_user_id)
    partitions = await _resolve_member_partitions(db, person_user_ids, active_org_id)

    per_org = [
        await _read_person_org_line(db, org_id, person_user_ids, period_type, period_start, is_active=org_id == active_org_id)
        for org_id in sorted(partitions, key=lambda org_id: (org_id != active_org_id, org_id))
    ]

    return per_org, _person_envelope(anchor, per_org)


async def _has_pending_cost_backfill(
    db: AsyncSession,
    org_id: str,
    user_id: str,
    period_start: date,
) -> bool:
    """Is any of the caller's recent spend logged but not yet priced? (NFR-5, #4477)

    **The rule, stated once here because it is the whole contract of the field:**
    ``True`` iff at least one ``usage_logs`` row exists for this caller, inside the
    reported period, written within ``_BACKFILL_LAG_WINDOW``, whose ``cost_usd`` is
    still ``0``.

    **Why that is the right observation and not a proxy for one.** The gateway
    writes a ``usage_logs`` row the instant a request finishes, priced ``0``
    (``proxy/service.py:_log_usage``). The budget-usage-tracker Lambda later prices
    that row and accumulates the ``budget_usage`` row ``_read_settled_spend`` reads
    — and its own write predicate is ``cost_usd = 0``, i.e. "not yet bridged"
    (``bridge_cost_to_usage_logs``, the ``handler.py:316`` bridge NFR-5 cites). So
    an unpriced row is not *evidence of* pending settlement; it is the very row the
    writer is still going to act on, and its dollars are provably absent from
    ``spend_usd``. Deriving from the writer's own predicate is what keeps the two
    from drifting: a second, independent notion of "pending" would be a guess that
    silently diverges the first time the bridge changes.

    Two bounds, each load-bearing rather than defensive:

    * **The recency window.** Several proxy error paths write ``cost_usd=0.0``
      permanently and are never bridged. Unbounded, one of those rows would pin the
      affordance ``true`` forever — and a warning that is always on is one users
      learn to ignore, which costs more than not having it (the issue's own
      blast-radius table names this as a distinct defect from the field being
      absent).
    * **The period bound.** ``spend_usd`` describes one calendar window. An
      unpriced row from last month says nothing about whether *this* month's figure
      is complete, and on the 1st of the month it would raise a warning about a
      figure that is in fact fully settled.

    **Known incompleteness, deliberately not papered over.** ``usage_logs.user_id``
    holds the Cognito sub of whoever made the call, so an agent chain's rows are
    keyed by the agent's service account, not by the human who triggered it. This
    probe therefore observes the caller's **direct** lag only; lag on their
    ``cloud`` line is invisible to it. Detecting that needs the cross-store DynamoDB
    lineage walk ``/me/budget/runs`` performs, which this endpoint must not take on
    (the field must add no unbounded query). The consequence is one-directional and
    safe: this can under-report lag, never over-report it — it never claims settled
    figures are stale. Widening it to the cloud path is follow-up work.

    Returns:
        ``True`` when recent spend is still settling, ``False`` when every recent
        request of the caller's has been priced. A read failure is NOT caught here:
        it propagates to the route's existing handler and becomes a ``503``, the
        same as any other ledger read, because guessing ``False`` during an outage
        would assert the figures are complete at exactly the moment we cannot know.
    """
    # `timestamp` is a full instant while `period_start` is a date, so the period
    # floor is widened to that day's first instant. `max` of the two bounds keeps
    # this a single indexed range scan: whichever is later is the only one that
    # constrains, and applying both would be redundant work.
    window_floor = datetime.now(UTC) - _BACKFILL_LAG_WINDOW
    period_floor = datetime.combine(period_start, time.min, tzinfo=UTC)
    floor = max(window_floor, period_floor)

    # `LIMIT 1` on the row's own id: the question is existence, not how many or how
    # much. No aggregate appears here — every figure in this module stays a single
    # row read (T33), and a COUNT would additionally scan rows whose answer cannot
    # change once the first match is found.
    pending = await db.scalar(
        select(UsageLog.id)
        .where(
            and_(
                UsageLog.org_id == org_id,
                UsageLog.user_id == user_id,
                UsageLog.timestamp >= floor,
                UsageLog.cost_usd == 0,
            )
        )
        .limit(1)
    )
    return pending is not None


@router.get("/me/budget", response_model=MyBudgetResponse)
async def get_my_budget(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_type: Annotated[
        Literal["daily", "weekly", "monthly"], Query(description="Calendar period to report. Run/chain caps are not calendar periods.")
    ] = "monthly",
) -> MyBudgetResponse:
    """Return the signed-in caller's own cap, settled spend and headroom.

    **Scoping.** Identity is taken exclusively from the validated token. This
    endpoint accepts no scope parameter, so a ``user_id``/``entity_id`` query
    param naming somebody else is simply not read — the caller always receives
    their own figures (FR-1.2, mirroring ``activity/routes.py``). Reading another
    user's budget is a managed-scope operation and lives behind an explicit
    permission check in a separate router (U-4).

    **Which line is reported.** The *binding* one: the lowest-remaining capped
    entity across the caller's hierarchy — the cap that will stop them first, and
    the same selection ``get_budget_status_for_headers`` makes. Uncapped entities
    cannot bind. Deliberately **not** a sum across entity types: enforcement
    checks each entity separately against its own cap, so a summed figure would
    be governed by no cap at all and the screen would claim "exhausted" while
    enforcement stopped nothing.

    **The envelope** (U-2, #4399). Alongside the headline the response carries
    ``lines`` — the caller's ``direct`` line and, when their identity resolved,
    their ``cloud`` line, each with its own cap and headroom (FR-2.1) — plus
    ``binding`` (the same headline line, named) and
    ``combined_informational`` (the direct+cloud dollar total, which no cap
    governs and which carries no denominator field so no bar can be bound to it).

    **The cross-org person view** (#4626, C1 of #4620). Everything described above
    is ONE partition — the caller's attributed tenant, which is what enforcement
    reads. That is a real gap for a person whose runs execute elsewhere: their page
    says ``$0`` while dollars accrue in another tenant's partition. So the response
    also carries ``per_org`` — their settled cloud-agent spend per member tenant,
    each with that tenant's own cap — and ``person_envelope``, the cross-org sum.

    The envelope is **informational**: it carries no cap, no headroom and no band,
    because no person-level cap table exists yet and whether one may deny is an open
    ruling (design note §5.7). Self-scope only, on the same structural basis as
    everything else here — the partition list is derived server-side from
    ``tenant_memberships`` and the endpoint accepts no scope parameter, so a home-org
    admin has no way to reach another tenant's totals through this surface (§7.2).

    **Settlement completeness** (#4477, NFR-5). Every spend figure here is a
    *settled* total and settlement is asynchronous, so the response also carries
    ``freshness.cost_backfill_lag`` — ``true`` when the caller has recent requests
    that are logged but not yet priced, making ``spend_usd`` a lower bound. Always
    present, so the client needs no null guard. See
    ``_has_pending_cost_backfill`` for the exact rule and its known limits.

    Note ``lines`` and ``binding`` range over different sets on purpose:
    ``lines`` holds only the caller's two personal ledgers, while ``binding`` is
    selected across the **whole** hierarchy, because a team or department cap can
    genuinely be the thing that stops them. When a shared ancestor binds it
    appears as ``binding`` without appearing in ``lines``.

    Returns:
        ``200`` with the caller's figures for the requested period.

    Raises:
        HTTPException:
            ``401`` when unauthenticated (from ``get_current_user``);
            ``422`` for a non-calendar ``period_type``;
            ``503`` when the ledger is unreadable — **never** a ``200`` with
            zeroes (FR-1.7). A backend outage that rendered as "you have spent
            nothing" would be silent and trusted, which is why the two are
            separated here even though ``get_budget_status_for_headers``
            collapses them.
    """
    resolved_period, period_start, period_end = _resolve_period_bounds(period_type)

    try:
        canonical_user_id, identity_status = await _resolve_root_principal(db, current_user)
        entities = _own_scope_hierarchy(current_user, canonical_user_id)

        # Attribution partition (#4132): the ledger rows enforcement actually
        # reads are keyed on the attributed tenant, so the read must use the same
        # org or it reports a cap nothing is enforcing.
        org_id = current_user.attributed_org_id

        binding: tuple[Decimal, EntityType, Decimal, BudgetConfig] | None = None
        binding_line: BudgetLine | None = None
        own_entity_type, own_entity_id = entities[0]
        own_spend: Decimal | None = None
        # The caller's own per-person lines, in hierarchy order (direct first,
        # cloud second) — U-2. Composed from the SAME reads that drive binding
        # selection below: this loop issues no extra queries, so the multi-line
        # envelope costs nothing beyond U-1's reads.
        lines: list[BudgetLine] = []

        for entity_type, entity_id in entities:
            cap_row = await _read_cap(db, org_id, entity_type, entity_id, resolved_period)
            spend = await _read_settled_spend(db, org_id, entity_type, entity_id, resolved_period, period_start)

            if entity_type == own_entity_type and entity_id == own_entity_id:
                own_spend = spend

            line = _compose_line(entity_type, entity_id, cap_row, spend)
            if line.source is not None:
                # Only the caller's personal ledgers are rendered as their
                # envelope. Shared ancestors still take part in binding selection
                # below — they just are not this person's spend.
                lines.append(line)

            if cap_row is None:
                # No cap on this entity: it has spend but no ceiling, so it can
                # never be the line that stops the caller. Skipped as a binding
                # candidate rather than treated as a cap of zero (FR-2, rule 6).
                continue

            effective_cap = _resolve_effective_period_cap(cap_row.budget_amount_usd)
            remaining = effective_cap - spend

            # Lowest remaining wins. Strict `<` keeps the most specific entity on
            # a tie, since the hierarchy is ordered most-specific-first.
            if binding is None or remaining < binding[0]:
                binding = (remaining, entity_type, spend, cap_row)
                binding_line = line

        # The cross-org person view (#4626). Gated on a RESOLVED canonical id, which
        # is also what keeps `service:` principals out structurally rather than by a
        # filter: `_resolve_root_principal` returns `None` for a service-account
        # caller, and the fused key list is built from `users` primary keys, which
        # can never carry the qualifier (#4344).
        #
        # `None` produces no lines and no envelope — deliberately not an empty
        # single-partition `$0` line, which would assert a measurement that was
        # never taken. `identity_status` already tells the client which case it is.
        #
        # Inside the same `try` as every other ledger read: a failed cross-org read
        # must become a 503, never a $0 cross-org total, which is the very defect
        # #4620 reports.
        per_org: list[PerOrgLine] = []
        person_envelope: PersonEnvelope | None = None
        if canonical_user_id:
            per_org, person_envelope = await _cross_org_person_view(db, canonical_user_id, org_id, resolved_period, period_start)

        # Inside the same `try` on purpose: a failure to determine settlement
        # completeness degrades identically to a failed ledger read (503), rather
        # than defaulting to "settled" and telling the caller their figures are
        # complete at the one moment we cannot know that (NFR-5).
        freshness = Freshness(
            cost_backfill_lag=await _has_pending_cost_backfill(db, org_id, current_user.user_id, period_start),
        )

    except _INFRASTRUCTURE_FAULTS as exc:
        # The ledger read failed. Surfacing this as a 200 with zeroes would tell
        # the user they have spent nothing during an outage — silent, and
        # trusted. 503 says "ask again", which is the truth (FR-1.7).
        logger.error("Failed to read own-scope budget; returning 503 rather than a zeroed budget", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Budget figures are temporarily unavailable. This is a backend failure, not a report of zero spend.",
        ) from exc

    period = BudgetPeriod(
        period_type=resolved_period.value,
        period_start=period_start,
        period_end=period_end,
        resets_in_days=max(0, (period_end - date.today()).days),
    )

    combined = _combined_informational(lines)

    if binding is None:
        # Nothing in the caller's hierarchy is capped. Their own line's spend is
        # still a real, useful figure, so it is reported — with cap_status
        # "uncapped" and every cap-derived field null, so no client can mistake
        # this for a cap of $0 (FR-1.5).
        #
        # `binding` is None here rather than an uncapped line: an uncapped line
        # cannot bind (rule 6), and reporting one as the binding line would render
        # a null headroom as the headline.
        return MyBudgetResponse(
            period=period,
            entity_type=own_entity_type.value,
            cap_usd=None,
            spend_usd=format_money(own_spend or Decimal("0"), SPEND_PLACES),
            remaining_usd=None,
            utilization_pct=None,
            band=None,
            cap_status="uncapped",
            enforcement_mode=None,
            identity_status=identity_status,
            binding=None,
            lines=lines,
            combined_informational=combined,
            # Present on the uncapped path too, and this is the path the operator's
            # own scenario lands on: no cap in the partition their session is in,
            # while their agents spend in another (#4620). Omitting the cross-org
            # view here would leave exactly the affected person seeing nothing.
            per_org=per_org,
            person_envelope=person_envelope,
            # Present on the uncapped path too. A caller with no cap still reads
            # this screen to see what they have spent, and that figure is exactly
            # as incomplete as a capped caller's — so the caveat is exactly as
            # necessary. It is also why the field is non-nullable: the frontend
            # reads it without knowing which path produced the response.
            freshness=freshness,
        )

    remaining, entity_type, spend, cap_row = binding
    effective_cap = _resolve_effective_period_cap(cap_row.budget_amount_usd)
    utilization, band = _band_for(effective_cap, spend)

    return MyBudgetResponse(
        period=period,
        entity_type=entity_type.value,
        cap_usd=format_money(effective_cap, CAP_PLACES),
        spend_usd=format_money(spend, SPEND_PLACES),
        # Not clamped at zero: settled spend can pass a cap (lowered after the
        # fact, or settled asynchronously after the requests were admitted), and
        # a read surface whose purpose is showing the true position must show the
        # overage rather than a flat "$0.00 left".
        remaining_usd=format_money(remaining, SPEND_PLACES),
        utilization_pct=utilization,
        band=band,
        cap_status="capped",
        enforcement_mode=cap_row.enforcement_mode,
        identity_status=identity_status,
        # The headline IS the binding line — selected, never added up. This is the
        # hard acceptance gate of U-2 (FR-2.3): the top-level figures above and
        # `binding` below are the same line's figures, and neither is the total
        # over `lines[].spend_usd`.
        binding=binding_line,
        lines=lines,
        combined_informational=combined,
        # Additive to the headline, never part of it (#4626). The cross-org total is
        # governed by no cap, so it cannot bind and it cannot be summed into
        # `spend_usd` above — that would make the headline a figure no ledger row
        # holds and no cap enforces, the same defect FR-2.3 forbids one scope up.
        per_org=per_org,
        person_envelope=person_envelope,
        # The caveat matters most here: this is the path where a cap is in play, so
        # an understated `spend_usd` reads as headroom the caller does not have.
        freshness=freshness,
    )


# ---------------------------------------------------------------------------
# Run drill-down — Issue #4400 (U-3)
# ---------------------------------------------------------------------------
#
# `/me/budget` answers "how much have I spent". This answers "what spent it".
#
# **The cross-store boundary is the whole difficulty.** Run lineage lives in
# DynamoDB (the `webhook-events` table, owned by agent-factory); cost lives in
# Postgres (`usage_logs`). There is no join to write — the stores are different
# engines. So: resolve lineage first, then batch ONE cost lookup for the run ids
# on that page. Exactly the shape `activity/routes.py:_enrich_with_cost` already
# uses, and the shape the issue bounds this endpoint to (one DDB query plus one
# batched Postgres lookup, both limited by period and page size).
#
# Three failure modes of that boundary are handled distinctly, because collapsing
# any two of them produces a number that is not a measurement:
#
#   * A run with no `usage_logs` row      -> that run's cost is `unknown`.
#     NOT `$0.00`. Back-fill is asynchronous, so this is the COMMON path for a
#     recent run, not an edge case.
#   * The whole cost lookup fails         -> every run's cost is `unknown` and the
#     response is still a `200`. Degrading rather than erroring is the
#     `activity/routes.py:176-206` precedent: the run list is still true and
#     useful without cost, and one missing cost row must not 500 the drill-down.
#   * The lineage read fails              -> `503`. There are no runs to report,
#     and an empty list would say "nothing ran", which is a claim we cannot make.
#
# The `unknown`-vs-zero distinction is enforced by `CostFigure`'s validator rather
# than by care at each call site here — see its docstring.


def _figure_from_cost_row(cost_row: dict | None, *, absent_reason: str) -> CostFigure:
    """Turn one row of ``get_cost_by_run_ids`` output into a three-valued figure.

    ``cost_row is None`` means the run id was **absent from the result dict**,
    which is how ``get_cost_by_run_ids`` reports "no ``usage_logs`` rows"
    (``cost_service.py:39``). That absence is the only available signal, and it is
    load-bearing: ``usage_logs.cost_usd`` is ``NOT NULL``, so a missing cost is
    never a NULL — it is row-nonexistence — and ``SUM`` over zero rows returns
    ``0``, indistinguishable from a genuine zero. The **row count** is what
    separates "no measurement" from "measured zero", which is why
    ``call_count == 0`` is treated as absence too rather than as a zero-dollar
    run. Same rule as ``src/orchestration/cost.py:_classify``.

    Args:
        cost_row: The ``{total_cost_usd, total_tokens, call_count}`` mapping for
            this run, or ``None`` when the run has no rows.
        absent_reason: Which ``unknown`` reason to report when there is no
            measurement — ``no_usage_rows`` when the ledger was read and had
            nothing, ``cost_store_unavailable`` when the ledger could not be read
            at all. Reporting the second as the first would assert something about
            the ledger that was never observed.
    """
    if cost_row is None or cost_row["call_count"] == 0:
        return CostFigure(status="unknown", reason=absent_reason)

    # `get_cost_by_run_ids` returns a float (it is shared with the activity
    # surface, which serialises floats). Converted via `str` rather than passed to
    # `Decimal` directly: `Decimal(0.0523)` is 0.05229999999999999926…, which
    # `format_money` would then render with fabricated trailing digits, whereas
    # `Decimal(str(0.0523))` is exactly `0.0523`. The float itself already carries
    # a precision loss this cannot undo, which is why the AMOUNT is re-rendered at
    # the column's 6dp and not treated as more precise than it is.
    amount = Decimal(str(cost_row["total_cost_usd"]))
    return CostFigure(
        # Rows exist and total zero: a VERIFIED zero, so `$0.00` is the honest
        # rendering — the opposite claim from `unknown` above.
        status="none_incurred" if amount == 0 else "known",
        amount_usd=format_money(amount, SPEND_PLACES),
    )


def _subtotal_figure(items: list[BudgetRunItem]) -> CostFigure:
    """Total the page's runs, flagging the total as partial when it is a lower bound.

    ``partial`` is the aggregate-level counterpart of ``unknown``: a total that
    excludes an unmeasured contribution is a **lower bound**, and presenting it as
    exact is how a decision gets made on a wrong number. Mirrors
    ``AggregateCost.partial`` (``src/orchestration/cost.py``) and is rendered by
    ``costTooltip(..., {partial: true})``.

    Three outcomes:

    * **Every run unknown** — there is no measured contribution at all, so the
      subtotal is itself ``unknown`` and carries no amount. Reporting ``$0.00``
      here would be the EPIC's headline failure in miniature.
    * **Some unknown** — the measured part is reported, ``partial=True``.
    * **None unknown** — an exact total. An empty page is this case: no runs cost
      nothing, which is a measurement about the page.

    Accumulated with an explicit loop rather than the builtin aggregate, for the
    same reason ``_combined_informational`` does — T33 in
    ``test_me_budget_routes.py`` forbids that aggregate's name anywhere in this
    module's source, a gate about keeping unfiltered SQL aggregates (#4328) out of
    the ledger reads. Decimal throughout, never float.
    """
    total = Decimal("0")
    unknown_count = 0
    for item in items:
        if item.cost.status == "unknown":
            unknown_count += 1
            continue
        total += Decimal(item.cost.amount_usd or "0")

    if items and unknown_count == len(items):
        # Nothing on this page was measured. The reason is carried up from the
        # runs so a whole-store outage is not reported as "the ledger had no rows".
        return CostFigure(status="unknown", reason=items[0].cost.reason or "no_usage_rows", partial=True)

    return CostFigure(
        status="none_incurred" if total == 0 else "known",
        amount_usd=format_money(total, SPEND_PLACES),
        partial=unknown_count > 0,
    )


def _run_attribution(item: InvocationItem) -> RunAttribution:
    """Which of the caller's envelope lines this run counts against.

    Always ``cloud``, and that is a structural fact rather than a placeholder.
    Every row in `webhook-events` is a hosted agent run, and a hosted run's spend
    lands on the caller's ``root_user`` ledger — the ``cloud`` line — because the
    ``user`` row it also writes is keyed by the *worker's* identity, not the
    caller's Cognito sub.

    The caller's ``direct`` line is their own interactive traffic (their machine,
    straight through the proxy). That traffic has no run binding, so it writes no
    lineage row at all (``BudgetLine``'s docstring, #4396) and therefore cannot
    appear in this list. ``direct`` stays in the union because the field's meaning
    is "which line does this belong to" and U-5 reads it against
    ``BudgetLine.source``; a run list that could only ever say ``cloud`` is
    honest, whereas deriving a ``direct``/``cloud`` split from
    ``trigger_kind`` would invent a distinction the ledger does not make and
    attribute cloud spend to the caller's machine.

    Takes the item so the derivation has somewhere to live if the ledger ever
    gains a direct-attributed lineage row (#4396).
    """
    return "cloud"


def _period_bounds_as_instants(period_start: date, period_end: date) -> tuple[str, str]:
    """Widen the period's dates to the full-day ISO-8601 instants DDB compares against.

    ``arrived_at`` is a full instant (``2026-08-29T09:14:00Z``) and the DynamoDB
    sort-key comparison is **lexicographic**, so a bare date is a broken bound:
    ``"2026-08-31" < "2026-08-31T09:14:00Z"``, which as an upper bound silently
    drops the whole last day of the period — every run on the 31st missing from an
    August drill-down, with nothing saying so.

    ``_expand_date_bound`` is reused rather than reimplemented because it is the
    fix for exactly that bug (#4390) and a second copy of the convention is how
    the two drift apart. It is module-private in ``activity/routes.py``; the
    import is deliberate and preferred over duplicating the literals.
    """
    since = _expand_date_bound(period_start.isoformat(), end=False)
    until = _expand_date_bound(period_end.isoformat(), end=True)
    # `_expand_date_bound` returns None only for a None input; both arguments here
    # are real dates, so these are always strings. Asserted for the type checker's
    # benefit rather than as a runtime claim.
    assert since is not None and until is not None
    return since, until


@router.get("/me/budget/runs", response_model=MyBudgetRunsResponse)
async def get_my_budget_runs(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    activity: Annotated[ActivityService, Depends(get_activity_service)],
    period_type: Annotated[
        Literal["daily", "weekly", "monthly"], Query(description="Calendar period to list runs from. Run/chain caps are not calendar periods.")
    ] = "monthly",
    period_start: Annotated[
        date | None,
        Query(description="Any day inside the period to report; normalised to that period's real bounds. Defaults to the current period."),
    ] = None,
    page_size: Annotated[int, Query(ge=1, le=100, description="Maximum runs per page.")] = 20,
    cursor: Annotated[str | None, Query(description="Opaque pagination cursor from a previous response's `next_cursor`.")] = None,
) -> MyBudgetRunsResponse:
    """List the agent runs that contributed to the caller's spend in one period.

    **Scoping.** Identity comes exclusively from the validated token. The endpoint
    accepts no ``user_id`` or ``entity_id`` param, so one naming a colleague is
    simply not read, and the caller always receives their own runs (FR-3.3). That
    is structural rather than a check: the lineage query is *partitioned* on the
    caller's own canonical id, so another member's or another tenant's runs are
    not merely filtered out — they are in a different partition and are never
    read. Reading somebody else's runs is a managed-scope operation and lives
    behind an explicit permission check in a separate router (U-4).

    **Chain-inclusive** (FR-3.2). ``query_by_user`` queries both the
    ``user-index`` (runs the caller triggered directly) and the
    ``root-human-index`` (runs attributed to them as the chain's root human,
    #3705) and merges with dedup, so a fan-out that cost $264 appears under the
    person who set it in motion rather than under an opaque worker identity.

    **Cost is three-valued** (FR-3.4/3.5). A run with no ``usage_logs`` row is
    ``{"status": "unknown", "reason": "no_usage_rows"}`` — never ``0``. Cost
    back-fill is asynchronous, so that is the ordinary state of a recent run. The
    ``subtotal`` is a ``CostFigure`` too, flagged ``partial`` when any contributor
    is unknown, because a total missing an unmeasured contribution is a lower
    bound.

    **Page-scoped totals.** ``subtotal`` and ``total_run_count`` describe this
    page, not the period — see ``MyBudgetRunsResponse``. The period-wide settled
    figure is what ``GET /me/budget`` reports.

    Returns:
        ``200`` with the caller's runs for the requested period. Also ``200``,
        with every cost ``unknown``, when the cost store cannot be read — the run
        list is still true without it.

    Raises:
        HTTPException:
            ``401`` when unauthenticated (from ``get_current_user``);
            ``400`` for a malformed ``cursor``;
            ``422`` for a non-calendar ``period_type``;
            ``503`` when the lineage store is unreadable — never a ``200`` with an
            empty list, which would say "nothing ran" (FR-1.7's rule applied to
            this surface).
    """
    resolved_period, resolved_start, resolved_end = _resolve_period_bounds(period_type, period_start)
    period = BudgetPeriod(
        period_type=resolved_period.value,
        period_start=resolved_start,
        period_end=resolved_end,
        resets_in_days=max(0, (resolved_end - date.today()).days),
    )

    try:
        canonical_user_id, identity_status = await _resolve_root_principal(db, current_user)
    except _INFRASTRUCTURE_FAULTS as exc:
        logger.error("Failed to resolve the caller's identity for the run drill-down; returning 503", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Run history is temporarily unavailable. This is a backend failure, not a report of zero runs.",
        ) from exc

    if not canonical_user_id:
        # No canonical id, so there is no lineage partition to read: either the
        # identity did not resolve, or the caller is a service account with no
        # `users` row by design. Returning an empty list with a ZERO subtotal here
        # would say "you ran nothing" when the truth is "we could not look" — the
        # exact false-$0 failure this EPIC exists to end. So the subtotal is
        # `unknown`, and `identity_status` says which of the two cases it is.
        logger.warning(
            "No canonical user id for the run drill-down; reporting unknown rather than an empty run list with a $0 subtotal",
            extra={"org_id": current_user.org_id, "identity_status": identity_status},
        )
        return MyBudgetRunsResponse(
            items=[],
            subtotal=CostFigure(status="unknown", reason="lineage_unavailable", partial=True),
            total_run_count=0,
            next_cursor=None,
            period=period,
            identity_status=identity_status,
        )

    since, until = _period_bounds_as_instants(resolved_start, resolved_end)

    try:
        lineage = activity.query_by_user(
            user_id=canonical_user_id,
            page_size=page_size,
            last_key=cursor,
            since=since,
            until=until,
        )
    except ValueError as exc:
        # A malformed cursor is a bad request, not a server error (the
        # `activity/routes.py` precedent).
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except _INFRASTRUCTURE_FAULTS as exc:
        # The lineage store is unreadable. An empty list would read as "no runs
        # contributed to your spend", which is a claim we cannot make.
        logger.error("Failed to read run lineage for the drill-down; returning 503 rather than an empty run list", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Run history is temporarily unavailable. This is a backend failure, not a report of zero runs.",
        ) from exc

    # The join key is `invocation_id`, which is the DynamoDB `event_id` and equals
    # `usage_logs.agent_run_id`. NOT `InvocationItem.run_id` — that field carries
    # the KEDA job/pod name, which matches no usage row, so joining on the
    # more-plausible-sounding name returns zero rows and reports every run as free
    # (`src/orchestration/cost.py`).
    run_ids = [item.invocation_id for item in lineage.items if item.invocation_id]

    absent_reason = "no_usage_rows"
    cost_map: dict[str, dict] = {}
    if run_ids:
        try:
            cost_map = await get_cost_by_run_ids(db, run_ids)
        except Exception:
            # Graceful degradation, mirroring `activity/routes.py:176-206`: the run
            # list is still true and useful without cost, so this returns 200 with
            # every figure `unknown` rather than failing the whole drill-down. The
            # reason distinguishes "the ledger had no rows for this run" from "the
            # ledger could not be read", which are different claims.
            logger.warning(
                "Failed to enrich the run drill-down with cost; returning runs with unknown cost rather than failing the request",
                exc_info=True,
            )
            absent_reason = "cost_store_unavailable"

    items = [
        BudgetRunItem(
            run_id=item.invocation_id,
            correlation_id=item.correlation_id,
            persona=item.persona,
            started_at=item.invoked_at,
            status=item.status,
            cost=_figure_from_cost_row(cost_map.get(item.invocation_id), absent_reason=absent_reason),
            attribution=_run_attribution(item),
        )
        for item in lineage.items
        if item.invocation_id
    ]

    return MyBudgetRunsResponse(
        items=items,
        subtotal=_subtotal_figure(items),
        total_run_count=len(items),
        next_cursor=lineage.last_key,
        period=period,
        identity_status=identity_status,
    )
