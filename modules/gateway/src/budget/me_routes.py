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
of the ledger, not of this read.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity import resolve_canonical_user_id
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

from .config import budget_config
from .enforcement_service import _INFRASTRUCTURE_FAULTS
from .schemas import (
    CAP_PLACES,
    SPEND_PLACES,
    BudgetBand,
    BudgetPeriod,
    MyBudgetResponse,
    format_money,
)
from .utils import CALENDAR_PERIOD_TYPES, calculate_budget_utilization, get_period_start_end

logger = logging.getLogger("bedrockgateway.budget")

# No `/api` prefix: CloudFront strips the first `/api` segment before the origin,
# so a router mounting under `/api/...` is unreachable through the dashboard
# (issue #4330, guarded by tests/test_route_prefix_convention.py). The browser
# calls `/api/me/budget`; this router serves `/me/budget`.
router = APIRouter(tags=["budget"])


def _resolve_period_bounds(period_type: str) -> tuple[PeriodType, date, date]:
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
    period_start, period_end = get_period_start_end(resolved)
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
        own_entity_type, own_entity_id = entities[0]
        own_spend: Decimal | None = None

        for entity_type, entity_id in entities:
            cap_row = await _read_cap(db, org_id, entity_type, entity_id, resolved_period)
            spend = await _read_settled_spend(db, org_id, entity_type, entity_id, resolved_period, period_start)

            if entity_type == own_entity_type and entity_id == own_entity_id:
                own_spend = spend

            if cap_row is None:
                # No cap on this entity: it has spend but no ceiling, so it can
                # never be the line that stops the caller. Skipped as a binding
                # candidate rather than treated as a cap of zero.
                continue

            effective_cap = _resolve_effective_period_cap(cap_row.budget_amount_usd)
            remaining = effective_cap - spend

            # Lowest remaining wins. Strict `<` keeps the most specific entity on
            # a tie, since the hierarchy is ordered most-specific-first.
            if binding is None or remaining < binding[0]:
                binding = (remaining, entity_type, spend, cap_row)

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

    if binding is None:
        # Nothing in the caller's hierarchy is capped. Their own line's spend is
        # still a real, useful figure, so it is reported — with cap_status
        # "uncapped" and every cap-derived field null, so no client can mistake
        # this for a cap of $0 (FR-1.5).
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
    )
