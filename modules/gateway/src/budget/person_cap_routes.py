"""Person-level cap authoring API — Issue #4629 (#4620 · C3).

Design note ``docs/design-notes/4620-cross-org-person-budgets.md`` §4.

A person gets **one** ceiling on their total agent spend, platform-wide — not one
per org. Until now there was nowhere to store such a number: every budget row
belongs to a tenant, so a cap authored in one partition caps nothing that executes
in another (#4620). This router is the authoring surface for
``person_budget_configs``, the partition-free table that fixes that.

**Enforcing since C4 (#4630).** A cap authored here now DENIES: the ruling on
§5.7 landed, and ``BudgetEnforcementService._check_person_budget`` reads this table
against a cross-org settled denominator, so the limit stops the person's agents in
every org they run in. ``enforcement_mode`` is written ``hard`` and remains
non-client-settable.

Two properties of that enforcement matter to anyone reading a figure from here:

* **The denominator is the settled ledger, so the cap is a bounded ceiling, not an
  atomic one.** No reservation key is taken for the person layer — a person key
  cannot carry the ``{org_id}`` Redis Cluster hash tag the atomic Lua needs
  (§5.5), and the #4620 ruling forbids a second non-atomic call. The overshoot
  bound is stated in ``docs/budget-ratelimit.md``.
* **Rows authored before C4 keep their stored ``soft`` mode and stay
  informational.** C3's UI promised those users that requests would not be
  blocked; converting the row on deploy would break that promise silently.
  Re-authoring (the ``PUT`` upsert) writes ``hard``.

Per-org caps are untouched by all of this and remain independently authoritative.

**Why a fourth budget router.** ``src/budget/routes.py`` reads
``entity_type``/``entity_id`` unscoped from the request and is open IDOR #4384, so
nothing scoped may live beside it. ``me_routes.py`` documents itself as read-only
(NFR-2) and every figure in it is a single ledger row read — adding writes there
would break a stated guarantee its tests rely on. ``managed_scope_routes.py`` is
the operator READ surface. This module is the only one that WRITES a
person-scoped cap, and the authoring rules it enforces (§4.2) are unlike any of
the three, so it gets its own file and its own tests.

## The authoring rule, and why the split is the whole point (§4.2)

| Author | May set a person-level cap? | Why |
|---|---|---|
| The person themselves | **Yes** | Self-restraint over their own agents. No cross-tenant authority is exercised. |
| Platform admin | **Yes** | Already holds cross-org authority by design. |
| An org admin — including the person's home org | **NO** | This is an authority inversion: an org admin's reach stops at their own partition's cap. |

That last row is the security property of this unit. A person-level cap is
partition-free, so if an org admin could author one they would be reaching into
every *other* tenant that person works in — a tenant they have no membership in,
cannot see, and cannot be audited by. The ruling on #4620 forbids it.

It is enforced **structurally**, in two layers rather than by one check that could
be dropped:

1. **The self path takes no target.** ``/me/budget/person-cap`` has no anchor
   parameter at any position — the anchor is derived from the token by
   ``resolve_caller_person_anchor``. There is no parameter for an org admin (or
   anybody) to point at somebody else, so no check is what stops them; the shape
   of the route is. Same argument ``me_routes.py`` makes for the read path.
2. **The targeted path is platform-admin-only.** ``/budget/person-cap/{anchor}``
   is the only route that names a person, and it is gated on
   ``AccessControl.require_platform_admin`` — which is the ONE role that skips the
   org-scope block by design. An org admin reaching it gets a ``403``.

Note what is deliberately NOT used: ``check_permission(BUDGET_UPDATE,
target_org_id=...)``. It authorises against an *org*, and an org admin holds
``BUDGET_UPDATE`` inside their own — so passing their own org id would make the
check trivially true and hand exactly the wrong party the authority. There is no
org that owns a partition-free row, so there is no org id that could correctly be
passed. ``require_platform_admin`` is a claim about the caller, not about a
partition, and that is what this table needs.

## Reads

The self read is included on the same router: a person authoring their own limit
needs to see the current one, and it is the same derivation and the same
scoping-by-shape as the write. The platform-admin read exists for the same reason
one level up. Neither read returns spend — that is C1 (#4626), whose
``person_envelope`` renders the cap against a cross-org denominator. A second
spend figure derived here is how two surfaces come to disagree about the same
dollars (#4322).
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity import resolve_caller_person_anchor, resolve_canonical_user_id, resolve_person_anchor
from src.shared.models.base import new_uuid
from src.shared.models.budget import PersonBudgetConfig
from src.shared.schemas.auth import TokenContext

from .enforcement_service import _INFRASTRUCTURE_FAULTS
from .schemas import CAP_PLACES, PersonCapRequest, PersonCapResponse, format_money

logger = logging.getLogger("bedrockgateway.budget")

# No `/api` prefix: CloudFront strips the first `/api` segment before the origin,
# so a router mounting under `/api/...` is unreachable through the dashboard
# (#4330, guarded by tests/test_route_prefix_convention.py). The browser calls
# `/api/me/budget/person-cap`; this router serves `/me/budget/person-cap`.
#
# No shared prefix on the router either, because its two routes deliberately sit
# on DIFFERENT paths: `/me/*` is the parameterless self surface, `/budget/*` the
# targeted platform-admin one. Collapsing them under one prefix would suggest they
# share an authorisation model, and the entire point of this module is that they
# do not.
router = APIRouter(tags=["budget"])

# Issue #4630 (C4) flipped this from `soft` to `hard`. C3 pinned it to `soft`
# because nothing read the table; `_check_person_budget` now does, so a cap
# authored here denies in every org the person's agents run in (note §5.3).
#
# Still NOT client-settable, and the reason is the §5.6 opt-in structure rather
# than caution: the person authoring a limit on their own agents IS the opt-in,
# and the platform admin is the only other party §4.2 permits. There is no third
# author for whom a mode choice would mean anything, so exposing the column would
# add a way to author an inert cap and nothing else.
#
# Written as a constant rather than relying on the column default so the value is
# visible at the write site — the one thing a reviewer of this unit checks.
_ENFORCING_MODE = "hard"

# Calendar periods only. A run/chain cap is lifetime-scoped and has no calendar
# window (`budget/utils.get_period_start_end` raises for them), so there is no
# person-level equivalent — enforced by a `Literal` at the HTTP boundary, so
# FastAPI answers 422 before any handler code runs.
PersonCapPeriod = Literal["daily", "weekly", "monthly"]


def _uncapped(person_anchor: str, period_type: str) -> PersonCapResponse:
    """The explicit "no limit authored" response.

    A distinct shape rather than a zeroed one (contract rule 2 in
    ``schemas.py``): ``cap_usd=0.00`` would render as a person who may spend
    nothing, which is the opposite of what no row means.
    """
    return PersonCapResponse(
        person_anchor=person_anchor,
        period_type=period_type,
        cap_usd=None,
        cap_status="uncapped",
        enforcement_mode=None,
        updated_at=None,
    )


def _compose(row: PersonBudgetConfig) -> PersonCapResponse:
    """Render a stored row.

    ``cap_usd`` goes through ``format_money`` at ``CAP_PLACES`` — the same 2dp
    string treatment every other cap on this contract gets. A float here would
    lose the precision the ``NUMERIC(10,2)`` column holds, which is contract rule
    1 and the defect class migration 030 exists for.
    """
    return PersonCapResponse(
        person_anchor=row.person_anchor,
        period_type=row.period_type,
        cap_usd=format_money(Decimal(row.budget_amount_usd), CAP_PLACES),
        cap_status="capped",
        enforcement_mode=row.enforcement_mode,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


async def _read_cap_row(db: AsyncSession, person_anchor: str, period_type: str) -> PersonBudgetConfig | None:
    """Read the one row for this person and period.

    The predicate is exactly ``uq_person_budget_config``, so this can match at most
    one row — the same single-row discipline every read in ``me_routes.py`` keeps.
    There is no ``org_id`` filter to add here and adding one would be the bug: the
    absence of a partition from the key is what makes the cap cross-org.
    """
    return await db.scalar(
        select(PersonBudgetConfig).where(
            PersonBudgetConfig.person_anchor == person_anchor,
            PersonBudgetConfig.period_type == period_type,
        )
    )


async def _upsert_cap(
    db: AsyncSession,
    *,
    person_anchor: str,
    period_type: str,
    amount: Decimal,
    authored_by_user_id: str,
) -> PersonBudgetConfig:
    """Create or replace this person's limit for one period.

    A ``PUT`` upsert rather than a ``POST``-then-``PATCH`` pair: the resource is
    "this person's limit for this period", of which there is at most one by unique
    constraint, so a create/update split would give a client two ways to express
    one intent and a way to get a 409 for having already done it.

    Re-authoring updates in place, which keeps the row's ``id`` stable and its
    ``created_at`` honest (when the person first set a limit, not when they last
    changed the number).
    """
    row = await _read_cap_row(db, person_anchor, period_type)
    if row is None:
        row = PersonBudgetConfig(
            id=new_uuid(),
            person_anchor=person_anchor,
            period_type=period_type,
            budget_amount_usd=amount,
            enforcement_mode=_ENFORCING_MODE,
            authored_by_user_id=authored_by_user_id,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            # Two first-time PUTs raced (double-click, two tabs, an admin PUT
            # racing the self PUT): both read None, both INSERTed, and the loser
            # tripped `uq_person_budget_config`. Left uncaught it would fall into
            # `_INFRASTRUCTURE_FAULTS` (IntegrityError ⊂ SQLAlchemyError) and be
            # misreported as a 503 backend outage — "nothing has changed" — while
            # the winner stored exactly this limit. The loser retries as the
            # UPDATE it semantically was.
            await db.rollback()
            row = await _read_cap_row(db, person_anchor, period_type)
            if row is None:
                # The violation was not this race; let the fault mapping have it.
                raise
            row.budget_amount_usd = amount
            row.enforcement_mode = _ENFORCING_MODE
            row.authored_by_user_id = authored_by_user_id
            row.updated_at = datetime.now(UTC)
            await db.commit()
    else:
        row.budget_amount_usd = amount
        # Issue #4630: re-authoring upgrades a C3-era `soft` row to enforcing.
        #
        # This is the documented one-click remediation for the flag day, and the
        # reason those rows are NOT converted on deploy: C3's shipped UI told the
        # person in as many words that "requests are not blocked" when they typed
        # the number. Silently converting that row into a denial breaks the promise
        # the screen made. Re-saving is the person restating the limit against the
        # current copy, which now says it enforces.
        row.enforcement_mode = _ENFORCING_MODE
        # Re-stamped on every re-author: the audit question is who set the limit
        # that is in force now, not who set the first one ever.
        row.authored_by_user_id = authored_by_user_id
        # `onupdate` covers the ORM flush, but only when a column actually changed
        # — a re-author with the SAME amount would otherwise leave `updated_at`
        # stale and make the row look older than the decision behind it.
        row.updated_at = datetime.now(UTC)
        await db.commit()

    await db.refresh(row)
    return row


def _unavailable(exc: Exception) -> HTTPException:
    """Map a ledger fault to a 503 that cannot be read as "no limit".

    Same rule as the rest of the budget read surface (FR-1.7): returning the
    uncapped shape during an outage would tell a person they have no limit at the
    one moment we cannot know that — and, on the write side, would leave them
    believing a limit was stored when it was not.
    """
    logger.error("person_budget_configs access failed; returning 503 rather than an uncapped or silently-unsaved response", exc_info=True)
    return HTTPException(
        status_code=503,
        detail="Your personal spending limit is temporarily unavailable. This is a backend failure, not a report that no limit is set.",
    )


# ---------------------------------------------------------------------------
# Self-service — the person's own limit. NO target parameter exists here.
# ---------------------------------------------------------------------------


@router.get("/me/budget/person-cap", response_model=PersonCapResponse)
async def get_my_person_cap(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_type: PersonCapPeriod = "monthly",
) -> PersonCapResponse:
    """Return the caller's own platform-wide spending limit for one period.

    **Scoping.** The anchor is derived from the validated token, and this route
    accepts no anchor parameter, so a request naming somebody else is not merely
    denied — there is nothing to name. The caller always gets their own limit.

    No spend figure is returned: this reports the authored ceiling only. The
    cross-org denominator it is rendered against is C1 (#4626).

    Returns:
        ``200`` with the limit, or with ``cap_status="uncapped"`` when none is
        authored. ``uncapped`` is a positive statement that no row exists — never a
        ``0.00``.

    Raises:
        HTTPException:
            ``401`` when unauthenticated (from ``get_current_user``);
            ``422`` when the caller has no linked GitHub identity, so they have no
            cross-org key a limit could be stored against (from
            ``resolve_caller_person_anchor`` — a
            ``UnresolvablePersonAnchorError``, rendered by the app's
            ``BedrockGatewayError`` handler);
            ``503`` when the table is unreadable — never a ``200`` reading as "no
            limit set".
    """
    try:
        # Inside the try (review fix): resolution runs 1-2 DB queries, and a fault
        # there must map to the same curated 503 as a fault one statement later —
        # not an opaque 500. UnresolvablePersonAnchorError is a BedrockGatewayError,
        # not an _INFRASTRUCTURE_FAULTS member, so the 422 path is unaffected.
        anchor, _ = await resolve_caller_person_anchor(db, current_user.user_id)
        row = await _read_cap_row(db, anchor, period_type)
    except _INFRASTRUCTURE_FAULTS as exc:
        raise _unavailable(exc) from exc

    return _compose(row) if row is not None else _uncapped(anchor, period_type)


@router.put("/me/budget/person-cap", response_model=PersonCapResponse)
async def put_my_person_cap(
    request: PersonCapRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_type: PersonCapPeriod = "monthly",
) -> PersonCapResponse:
    """Set the caller's own platform-wide spending limit for one period.

    Self-restraint, which is why this needs no authorisation beyond
    authentication: the person is bounding the spend of agents they set in motion,
    exercising no authority over any tenant. §4.2's first row.

    **This limit ENFORCES (#4630).** The row is written ``hard``: the person's
    agents are denied in every org they run in once the cross-org settled total
    passes it. Authoring it is the §5.6 opt-in — the person choosing to be stopped,
    which is why self-authorship is what makes a denying cross-org cap legitimate.
    The ceiling is bounded rather than atomic (settled denominator, §5.5); see
    ``docs/budget-ratelimit.md`` for the overshoot bound.

    Idempotent: re-authoring replaces the amount in place, keeping the row's ``id``
    and ``created_at``. Re-authoring a C3-era ``soft`` row also upgrades it to
    ``hard`` — the documented remediation for limits typed under the old
    "requests are not blocked" copy.

    Returns:
        ``200`` with the stored limit.

    Raises:
        HTTPException:
            ``401`` when unauthenticated;
            ``422`` for a non-positive or over-precise amount (from the request
            model), a non-calendar ``period_type``, or a caller with no linked
            GitHub identity — in which case **nothing is written**, because a row
            keyed on an anchor no ledger row can carry is a limit that displays
            and governs nothing (#4511);
            ``503`` when the write fails — never a ``200``, which would leave the
            person believing a limit is in force.
    """
    try:
        anchor, canonical_user_id = await resolve_caller_person_anchor(db, current_user.user_id)
        row = await _upsert_cap(
            db,
            person_anchor=anchor,
            period_type=period_type,
            amount=request.budget_amount_usd,
            # The CANONICAL id, not TokenContext.user_id (review fix): the token id
            # is a Cognito sub on the ordinary JWT path and a canonical id on
            # #3989-rewritten paths, so persisting it raw would mix two namespaces
            # in an audit column whose contract is canonical users.id.
            authored_by_user_id=canonical_user_id,
        )
    except _INFRASTRUCTURE_FAULTS as exc:
        raise _unavailable(exc) from exc

    logger.info("person_cap_authored scope=self period=%s", period_type)
    return _compose(row)


@router.delete("/me/budget/person-cap", status_code=204)
async def delete_my_person_cap(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_type: PersonCapPeriod = "monthly",
) -> None:
    """Remove the caller's own platform-wide spending limit for one period.

    Removing a limit is a DELETE, not a ``PUT`` of ``0`` — ``0`` is a real ceiling
    of zero dollars and the request model rejects it for that reason.

    **204 whether or not a row existed.** The outcome a caller asked for ("I have
    no personal limit") holds either way, and a 404 for the already-absent case
    would make a retried delete look like a failure.

    Raises:
        HTTPException:
            ``401`` when unauthenticated;
            ``422`` when the caller has no linked GitHub identity;
            ``503`` when the delete fails — never a silent success, which would
            leave a limit in force that the person believes they removed.
    """
    try:
        anchor, _ = await resolve_caller_person_anchor(db, current_user.user_id)
        await db.execute(
            delete(PersonBudgetConfig).where(
                PersonBudgetConfig.person_anchor == anchor,
                PersonBudgetConfig.period_type == period_type,
            )
        )
        await db.commit()
    except _INFRASTRUCTURE_FAULTS as exc:
        raise _unavailable(exc) from exc

    logger.info("person_cap_deleted scope=self period=%s", period_type)


# ---------------------------------------------------------------------------
# Platform admin — the ONLY path that names a person. §4.2's second row.
# ---------------------------------------------------------------------------


@router.get("/budget/person-cap/{person_anchor}", response_model=PersonCapResponse)
async def get_person_cap(
    person_anchor: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_type: PersonCapPeriod = "monthly",
) -> PersonCapResponse:
    """Read any person's platform-wide limit. **Platform admin only.**

    An org admin gets a ``403`` here, including for a member of their own org, and
    that is the ruling on #4620, not an oversight: the row is partition-free, so
    reading it is reading a figure that governs the person's spend in every other
    tenant they work in — tenants the org admin has no membership in.

    Raises:
        HTTPException:
            ``401`` when unauthenticated;
            ``403`` for any caller who is not a platform admin (from
            ``require_platform_admin``, via the app's ``BedrockGatewayError``
            handler);
            ``422`` for a malformed anchor or a non-calendar ``period_type``;
            ``503`` when the table is unreadable.
    """
    # Authority first, before the anchor is resolved: a non-admin must not be able
    # to use the 422-vs-200 difference to learn whether a GitHub id is linked on
    # this platform.
    AccessControl(db).require_platform_admin(current_user)

    try:
        resolved_anchor = await resolve_person_anchor(db, person_anchor)
        row = await _read_cap_row(db, resolved_anchor, period_type)
    except _INFRASTRUCTURE_FAULTS as exc:
        raise _unavailable(exc) from exc

    return _compose(row) if row is not None else _uncapped(resolved_anchor, period_type)


@router.put("/budget/person-cap/{person_anchor}", response_model=PersonCapResponse)
async def put_person_cap(
    person_anchor: str,
    request: PersonCapRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_type: PersonCapPeriod = "monthly",
) -> PersonCapResponse:
    """Set any person's platform-wide limit. **Platform admin only.**

    The platform admin already holds cross-org authority by design (§4.2's second
    row), which is why this is the one targeted write that exists. An org admin is
    denied — see the module docstring for why ``check_permission(BUDGET_UPDATE,
    target_org_id=...)`` would be the wrong gate rather than a stricter one.

    The anchor is validated against a real linked GitHub identity before anything
    is written: a cap keyed on an id no ``user_identities`` row carries can never
    match a settled ledger row, so it would display a limit and govern nothing
    (#4511).

    Written ``hard``, exactly as the self path (#4630): this cap denies the
    person's agents in every org they run in. The platform admin is the one other
    party §4.2 permits to author it, holding cross-org authority by design.

    Raises:
        HTTPException:
            ``401`` when unauthenticated;
            ``403`` for any caller who is not a platform admin;
            ``422`` for a malformed or unlinked anchor, a non-positive/over-precise
            amount, or a non-calendar ``period_type`` — nothing is written in any
            of those cases;
            ``503`` when the write fails.
    """
    AccessControl(db).require_platform_admin(current_user)

    try:
        resolved_anchor = await resolve_person_anchor(db, person_anchor)
        row = await _upsert_cap(
            db,
            person_anchor=resolved_anchor,
            period_type=period_type,
            amount=request.budget_amount_usd,
            # Canonical id for the audit column — the admin's token id is a sub on
            # the JWT path (review fix; see put_my_person_cap).
            authored_by_user_id=await resolve_canonical_user_id(db, current_user.user_id),
        )
    except _INFRASTRUCTURE_FAULTS as exc:
        raise _unavailable(exc) from exc

    logger.info("person_cap_authored scope=platform_admin period=%s", period_type)
    return _compose(row)
