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

**What the number governs, since #4396: the person's TOTAL spend.** Per the
operator ruling of 2026-09-05, the limit authored here covers *everything* the
person spends — their own direct, interactive use **plus** the cloud agents they
trigger — summed across every GitHub org they belong to. It is deliberately not two
budgets: the person sees one figure on ``/me/budget`` (``person_envelope.spend_usd``)
and that figure is the denominator this cap is enforced against. Before #4396 the
cap governed cloud-agent spend only, so a person could sit inside their limit while
spending freely from their own machine.

The practical consequence for this surface: the ceiling stored here now stops
INTERACTIVE requests too, not just agent runs. A number chosen under the old
meaning governs strictly more spend than its author intended — which is why the
copy on the authoring UI names both halves.

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

## DEFAULTS, and the ceiling rule they impose (#4690 · D1)

Everything above is about ONE person's row. ``/budget/person-default/{scope}`` (added
by #4690) authors a **rule**: "$1,000/month each, unless we say otherwise", governing
every current and future member of a platform, org or team scope. Without it, "this
person has no row" meant "this person is unlimited", so bounding everybody required
a bulk write that still left every future joiner uncapped.

**Defaults are CEILINGS** (operator ruling, 2026-09-07), and that single word is what
this module has to enforce at the HTTP boundary, because nothing below it can:

* ``PUT /me/budget/person-cap`` now **rejects an amount above the applicable
  default with a 422 naming it**. Self-service is for restraint — lowering your own
  ceiling — and a self path that could raise it would make every default advisory.
* ``PUT /budget/person-cap/{anchor}`` does **not** apply that check. The platform
  admin authoring an individual row above a default IS the "unless we say otherwise"
  clause; it is the documented exception mechanism, not a loophole.
* ``GET /me/budget/person-cap`` reports the limit that **actually governs** the
  caller, with a ``source`` saying which rung it came from. A person held to a
  platform default they never authored used to be shown ``uncapped`` here while a
  402 stopped their requests — the screen/behaviour disagreement #4620 exists to
  close, reintroduced one rung up.

The defaults CRUD is gated exactly as ``/budget/person-cap/{anchor}``: platform admin
only, ``require_platform_admin``, for a stronger version of the same reason. An org
admin authoring an *org*-scoped default sounds locally reasonable, but the rule it
writes governs its members' spend in every OTHER tenant they work in — the authority
inversion §4.2 forbids. If per-org authorship is ever wanted it needs its own ruling,
not a relaxed gate here.

The resolution ladder itself is NOT in this module — it is
``person_ledger.resolve_applicable_person_limits``, the leaf both this router and
``BudgetEnforcementService`` import. That is deliberate and is the #4689 lesson: a
ceiling this surface validates against, computed differently from the one enforcement
denies on, is a 422 that disagrees with the 402 it exists to predict.
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity import resolve_caller_person_anchor, resolve_canonical_user_id, resolve_person_anchor
from src.shared.identity.person_anchor import UnresolvablePersonAnchorError
from src.shared.models.base import new_uuid
from src.shared.models.budget import PersonBudgetConfig, PersonBudgetDefault
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext

from .enforcement_service import _INFRASTRUCTURE_FAULTS
from .person_ledger import (
    PersonLimit,
    PersonLimitSource,
    resolve_applicable_person_limits,
    resolve_member_partitions,
    resolve_person_identity,
    resolve_person_team_keys,
)
from .schemas import (
    CAP_PLACES,
    PersonCapRequest,
    PersonCapResponse,
    PersonDefaultRequest,
    PersonDefaultResponse,
    format_money,
)

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

    Since #4690 this means "no rule of ANY kind applies" — no individual row and no
    default at any rung. ``source`` is ``None`` here for exactly that reason, and it
    is the only shape in which it is.
    """
    return PersonCapResponse(
        person_anchor=person_anchor,
        period_type=period_type,
        cap_usd=None,
        cap_status="uncapped",
        enforcement_mode=None,
        updated_at=None,
        source=None,
        source_label=None,
    )


def _compose(row: PersonBudgetConfig, *, source: PersonLimitSource = "admin") -> PersonCapResponse:
    """Render a stored INDIVIDUAL row.

    ``cap_usd`` goes through ``format_money`` at ``CAP_PLACES`` — the same 2dp
    string treatment every other cap on this contract gets. A float here would
    lose the precision the ``NUMERIC(10,2)`` column holds, which is contract rule
    1 and the defect class migration 030 exists for.

    Used where the row itself is the answer: the platform-admin read, and the
    response to a write (which by definition just stored an individual row). The
    self READ goes through ``_compose_limit`` instead, because there the question is
    which rule governs the caller, not what is in their row.

    Args:
        row: The stored individual limit.
        source: ``own`` where the caller is provably the author (the self ``PUT``
            just wrote it), otherwise ``admin``. Defaulted to ``admin`` rather than
            derived from ``authored_by_user_id``, because on the platform-admin read
            path the *reader* is not the person, so "own" would be a claim about
            somebody who is not present — and mistakenly reporting ``own`` tells a
            client the person may lower a number they may not. The ladder's
            ``self_authored_by=None`` case makes the same conservative choice.
    """
    return PersonCapResponse(
        person_anchor=row.person_anchor,
        period_type=row.period_type,
        cap_usd=format_money(Decimal(row.budget_amount_usd), CAP_PLACES),
        cap_status="capped",
        enforcement_mode=row.enforcement_mode,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        source=source,
        source_label="your own limit" if source == "own" else "a limit set for you by a platform administrator",
    )


def _compose_limit(person_anchor: str, limit: PersonLimit, updated_at: str | None) -> PersonCapResponse:
    """Render the limit that ACTUALLY governs a person — #4690.

    The self read's shape. ``cap_status`` is ``capped`` whether the number came from
    the person's own row or from a default they have never seen, because from the
    caller's point of view both stop their requests at the same figure — and the old
    behaviour (``uncapped`` whenever no individual row existed) is precisely the
    screen/behaviour disagreement this issue closes.

    ``source`` and ``source_label`` come off the ladder rather than being recomputed
    here, so this response, the 402's text and the 422's text all name the rung the
    same way.

    Args:
        person_anchor: The caller's own cross-org key, echoed back unchanged.
        limit: The applicable limit from ``resolve_applicable_person_limits``.
        updated_at: The individual row's timestamp when one is what applies, else
            ``None`` — a default's ``updated_at`` is deliberately NOT reported on
            this surface. It is the moment somebody else authored a rule about a
            population, which tells the person nothing and reads as if THEY had
            changed something.
    """
    return PersonCapResponse(
        person_anchor=person_anchor,
        period_type=limit.period_type,
        cap_usd=format_money(Decimal(limit.amount), CAP_PLACES),
        cap_status="capped",
        enforcement_mode=limit.enforcement_mode,
        updated_at=updated_at,
        source=limit.source,
        source_label=limit.scope_label,
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
# DEFAULT limits — the scope rules. Issue #4690 (D1).
# ---------------------------------------------------------------------------


async def _require_scope_exists(db: AsyncSession, scope_type: str, scope_id_org: str | None, scope_id_team: str | None) -> None:
    """Refuse a default aimed at a scope that does not exist (review fix on #4696).

    A rule scoped to a mistyped, foreign, or deleted tenant id stores cleanly,
    reads back "capped", and governs NOBODY — the #4511 inert-cap class on the
    brand-new governance surface, at whatever population the admin believed they
    bounded. An existence SELECT at write time is the cheap, lifecycle-decoupled
    alternative to the FK the migration deliberately rejected.

    Org: the ``organizations`` row must exist. Team: at least one ``users`` row
    must carry the (org, team) pair — teams live in Cognito attributes, so "a
    team someone is actually in" is the only existence a rule can usefully have;
    a team no user carries would govern nobody by construction.
    """
    if scope_type == "platform":
        return
    org_exists = await db.scalar(select(Organization.id).where(Organization.id == scope_id_org).limit(1))
    if org_exists is None:
        raise HTTPException(status_code=422, detail=f"No GitHub org with id '{scope_id_org}' exists on this platform; the rule would govern nobody.")
    if scope_type == "team":
        member_exists = await db.scalar(select(User.id).where(User.org_id == scope_id_org, User.team_id == scope_id_team).limit(1))
        if member_exists is None:
            raise HTTPException(
                status_code=422,
                detail=f"No member of GitHub org '{scope_id_org}' carries team id '{scope_id_team}'; the rule would govern nobody.",
            )


def _parse_scope(scope: str) -> tuple[str, str | None, str | None]:
    """Parse the path scope into ``(scope_type, scope_id_org, scope_id_team)``.

    The wire form is ``platform`` | ``org:<org_id>`` | ``team:<org_id>:<team_id>``.

    **One path segment rather than three query parameters**, because the scope is the
    identity of the resource being addressed: ``PUT /budget/person-default/org:acme``
    is a complete statement of what is being written, whereas ``PUT
    /budget/person-default?scope_type=org`` with a missing ``org_id`` is a request
    that has to be *rejected* rather than one that cannot be formed. Same reasoning
    ``/budget/person-cap/{anchor}`` uses for the anchor.

    The team form carries BOTH ids because a ``teams.id`` is unique only inside its
    org (``teams`` carries ``TenantMixin``); a team scope naming only the team would
    be a rule that could govern a same-id team in an unrelated tenant.

    Raises:
        HTTPException: ``422`` for any string that is not one of the three forms, or
            that has an empty id in any position. Rejected here rather than stored,
            because ``ck_person_budget_default_scope`` would refuse the row anyway
            and an IntegrityError from a *client* mistake surfaces as a 503 "backend
            failure" through this module's fault mapping — telling an admin to retry
            a request that can never succeed.
    """
    parts = scope.split(":")

    if parts == ["platform"]:
        return "platform", None, None
    if len(parts) == 2 and parts[0] == "org" and parts[1]:
        return "org", parts[1], None
    if len(parts) == 3 and parts[0] == "team" and parts[1] and parts[2]:
        return "team", parts[1], parts[2]

    raise HTTPException(
        status_code=422,
        detail=(
            "Scope must be 'platform', 'org:<org_id>', or 'team:<org_id>:<team_id>'. "
            "A team scope needs its organization id too, because a team id is unique "
            "only within its own organization."
        ),
    )


def _uncapped_default(scope_type: str, scope_id_org: str | None, scope_id_team: str | None, period_type: str) -> PersonDefaultResponse:
    """The explicit "no default authored for this scope" response.

    A distinct shape, not a zeroed one (contract rule 2): a ``0.00`` default would
    read as "nobody in this scope may spend anything" — the most restrictive possible
    rule where in fact there is none.
    """
    return PersonDefaultResponse(
        scope_type=scope_type,
        scope_id_org=scope_id_org,
        scope_id_team=scope_id_team,
        period_type=period_type,
        cap_usd=None,
        cap_status="uncapped",
        enforcement_mode=None,
        updated_at=None,
    )


def _compose_default(row: PersonBudgetDefault) -> PersonDefaultResponse:
    """Render a stored default rule, money as a 2dp string (contract rule 1)."""
    return PersonDefaultResponse(
        scope_type=row.scope_type,
        scope_id_org=row.scope_id_org,
        scope_id_team=row.scope_id_team,
        period_type=row.period_type,
        cap_usd=format_money(Decimal(row.budget_amount_usd), CAP_PLACES),
        cap_status="capped",
        enforcement_mode=row.enforcement_mode,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


def _default_row_predicate(scope_type: str, scope_id_org: str | None, scope_id_team: str | None):
    """The predicate matching exactly one default row.

    ``IS NULL`` rather than ``== None`` for the absent halves, spelled out because
    this predicate must line up with ``uq_person_budget_default`` — which indexes
    ``COALESCE(col, '')`` precisely because SQL NULL comparison does NOT behave like
    equality. Writing ``PersonBudgetDefault.scope_id_org == None`` here would match
    nothing and turn every re-author of a platform default into an INSERT that trips
    the unique index.
    """
    return (
        PersonBudgetDefault.scope_type == scope_type,
        PersonBudgetDefault.scope_id_org.is_(None) if scope_id_org is None else PersonBudgetDefault.scope_id_org == scope_id_org,
        PersonBudgetDefault.scope_id_team.is_(None) if scope_id_team is None else PersonBudgetDefault.scope_id_team == scope_id_team,
    )


async def _read_default_row(
    db: AsyncSession,
    scope_type: str,
    scope_id_org: str | None,
    scope_id_team: str | None,
    period_type: str,
) -> PersonBudgetDefault | None:
    """Read the one default rule for this scope and period, or ``None``."""
    return await db.scalar(
        select(PersonBudgetDefault).where(
            *_default_row_predicate(scope_type, scope_id_org, scope_id_team),
            PersonBudgetDefault.period_type == period_type,
        )
    )


async def _upsert_default(
    db: AsyncSession,
    *,
    scope_type: str,
    scope_id_org: str | None,
    scope_id_team: str | None,
    period_type: str,
    amount: Decimal,
    authored_by_user_id: str,
) -> PersonBudgetDefault:
    """Create or replace the default rule for one scope and period.

    Structurally identical to ``_upsert_cap``, including its ``IntegrityError``
    retry, and for the same reason (#4647): two first-time ``PUT``s that race both
    read ``None``, both ``INSERT``, and the loser trips ``uq_person_budget_default``.
    Left uncaught that becomes a 503 "backend failure — nothing has changed" while
    the winner stored exactly this rule, so the loser retries as the ``UPDATE`` it
    semantically was.

    Not factored into one generic upsert with ``_upsert_cap``: the two differ in
    their key, their table, their enforcement-mode rules and their re-author
    semantics (an individual row's ``soft``→``hard`` upgrade has no equivalent here),
    so the shared part would be the four lines of retry structure and the parameter
    list to abstract over it would be longer than the duplication.
    """
    row = await _read_default_row(db, scope_type, scope_id_org, scope_id_team, period_type)
    if row is None:
        row = PersonBudgetDefault(
            id=new_uuid(),
            scope_type=scope_type,
            scope_id_org=scope_id_org,
            scope_id_team=scope_id_team,
            period_type=period_type,
            budget_amount_usd=amount,
            enforcement_mode=_ENFORCING_MODE,
            authored_by_user_id=authored_by_user_id,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            row = await _read_default_row(db, scope_type, scope_id_org, scope_id_team, period_type)
            if row is None:
                # Not this race — e.g. a scope shape the CHECK constraint refused.
                # Let the fault mapping have it.
                raise
            row.budget_amount_usd = amount
            row.enforcement_mode = _ENFORCING_MODE
            row.authored_by_user_id = authored_by_user_id
            row.updated_at = datetime.now(UTC)
            await db.commit()
    else:
        row.budget_amount_usd = amount
        row.enforcement_mode = _ENFORCING_MODE
        # Re-stamped: the audit question is who set the rule in force NOW.
        row.authored_by_user_id = authored_by_user_id
        # `onupdate` only fires when a column actually changed, so a re-author with
        # the same amount would otherwise leave this stale.
        row.updated_at = datetime.now(UTC)
        await db.commit()

    await db.refresh(row)
    return row


def _default_unavailable(exc: Exception) -> HTTPException:
    """Map a defaults-table fault to a 503 that cannot be read as "no default".

    Its own message rather than ``_unavailable``'s, because the audience differs: an
    admin authoring a rule for a population needs to know their write did not land,
    and being told "YOUR personal spending limit is unavailable" would send them
    looking at the wrong screen.
    """
    logger.error("person_budget_defaults access failed; returning 503 rather than an uncapped or silently-unsaved response", exc_info=True)
    return HTTPException(
        status_code=503,
        detail="Default person limits are temporarily unavailable. This is a backend failure, not a report that no default is set.",
    )


async def _caller_scope_keys(db: AsyncSession, canonical_user_id: str, active_org_id: str) -> tuple[list[str], list[tuple[str, str]]]:
    """The caller's ``(org_ids, team_keys)`` — which scopes' default rules may govern them.

    Server-derived from the caller's fused identity, never from request input: this
    pair is what decides whose rules apply, so a caller able to influence it could
    nominate the scope with the most generous default.

    Args:
        db: Async session bound to the gateway DB.
        canonical_user_id: The caller's own canonical ``users.id``.
        active_org_id: ``TokenContext.org_id`` — the **authenticated** partition, not
            ``attributed_org_id``. The two agree for every non-internal caller, but
            the attributed one is documented as caller-influenced and must never gate
            anything.
    """
    _, person_user_ids = await resolve_person_identity(db, canonical_user_id)
    return (
        await resolve_member_partitions(db, person_user_ids, active_org_id),
        await resolve_person_team_keys(db, person_user_ids),
    )


@router.get("/me/budget/person-cap", response_model=PersonCapResponse)
async def get_my_person_cap(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_type: PersonCapPeriod = "monthly",
) -> PersonCapResponse:
    """Return the spending limit that GOVERNS the caller for one period.

    **Scoping.** The anchor is derived from the validated token, and this route
    accepts no anchor parameter, so a request naming somebody else is not merely
    denied — there is nothing to name. The caller always gets their own limit.

    **Since #4690 this reports the APPLICABLE limit, not just the caller's own row.**
    The ladder is individual row > team default > org default > platform default, and
    ``source`` says which rung supplied the number:

    | ``source`` | Means | Can the caller change it? |
    |---|---|---|
    | ``own`` | They authored this row (pre-ruling — self writes are gone) | No; ask a platform admin |
    | ``admin`` | A platform admin authored it for them | No; ask a platform admin |
    | ``*_default`` | No row of theirs exists; a team/org/platform rule governs them | No; ask a platform admin |

    ``cap_status`` is therefore ``capped`` when a default applies even though the
    person has authored nothing. Reporting ``uncapped`` there — the pre-#4690
    behaviour — told people they had no limit while a 402 stopped them at one, which
    is the screen/behaviour disagreement #4620 exists to close.

    No spend figure is returned: this reports the authored ceiling only. The
    cross-org denominator it is rendered against is C1 (#4626).

    Returns:
        ``200`` with the applicable limit, or with ``cap_status="uncapped"`` when
        NOTHING governs the caller for this period — no row and no default at any
        rung. ``uncapped`` is a positive statement to that effect, never a ``0.00``.

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
        try:
            anchor, canonical_user_id = await resolve_caller_person_anchor(db, current_user.user_id)
        except UnresolvablePersonAnchorError:
            # Review fix on #4696: a person with NO linked GitHub identity can hold
            # no individual row — but a DEFAULT still governs them, and enforcement
            # denies them at it. 422ing here reintroduced the #4620 class for
            # exactly the population defaults newly bring under enforcement: a 402
            # they could never see coming on the one surface built to show it.
            # Resolve the canonical id directly; the ladder runs with no top rung.
            anchor = None
            canonical_user_id = await db.scalar(
                select(User.id).where(or_(User.cognito_sub == current_user.user_id, User.id == current_user.user_id)).limit(1)
            )
            if canonical_user_id is None:
                # No users row at all — nothing can govern them; the original 422
                # ("not provisioned") is the honest answer.
                raise
        # The ladder decides which limit is reported, even when a row exists: an
        # individual row shadows the default for ITS period only, and only the
        # resolver knows the rung. Called with the caller's own canonical id so their
        # own row is reported as `own` rather than `admin` — the difference between
        # "the number is yours" and "ask a platform admin".
        org_ids, team_keys = await _caller_scope_keys(db, canonical_user_id, current_user.org_id)
        limits = await resolve_applicable_person_limits(
            db,
            person_anchor=anchor,
            org_ids=org_ids,
            team_keys=team_keys,
            self_authored_by=canonical_user_id,
        )
    except _INFRASTRUCTURE_FAULTS as exc:
        raise _unavailable(exc) from exc

    display_anchor = anchor or f"users:{canonical_user_id}"
    limit = limits.get(period_type)
    if limit is None:
        return _uncapped(display_anchor, period_type)
    # `updated_at` rides the PersonLimit itself now (review fix on #4696): the
    # duplicate row read this used to make existed only for this field, and two
    # independent reads could pair a new amount with a stale timestamp under a
    # concurrent write. Default rungs carry None — a default's timestamp is
    # deliberately withheld from person-facing surfaces.
    return _compose_limit(display_anchor, limit, limit.updated_at)


# The self-service WRITE routes were removed by the 2026-09-07 operator ruling:
# person limits are admin-governed only. `PUT`/`DELETE /me/budget/person-cap` no
# longer exist — deleted, not 403-stubbed, so the OpenAPI surface does not
# advertise a write that always denies. The GET above remains the person's
# read-only view (applicable limit + source). Route-absence is pinned by
# `test_person_cap_routes.py::TestSelfServiceWritesAreGone`.


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

    Written ``hard`` (#4630): this cap denies the person's requests and agent runs in every
    org. Since the 2026-09-07 ruling the platform admin is the ONLY party who
    authors individual rows — the self write routes no longer exist.

    **This route does NOT apply the #4690 default ceiling**, and that is the design,
    not an omission. A default is "everybody, unless we say otherwise"; a platform
    admin writing an individual row above one IS the "otherwise". Applying the
    ceiling here would leave no way to grant an exception at all — an admin would
    have to raise the default for everybody to raise it for one person, which is the
    opposite of what a default is for. With self-service removed, this
    route is the single, audit-stamped escape hatch.

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


# ---------------------------------------------------------------------------
# DEFAULT rules — platform admin only. Issue #4690 (D1).
#
# Same gate as `/budget/person-cap/{anchor}` above, for a stronger version of the
# same reason: an ORG-scoped default authored by that org's own admin still governs
# its members' spend in every OTHER tenant they work in, which is the authority
# inversion §4.2 forbids. See the module docstring.
# ---------------------------------------------------------------------------


@router.delete("/budget/person-cap/{person_anchor}", status_code=204)
async def delete_person_cap(
    person_anchor: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_type: PersonCapPeriod = "monthly",
) -> None:
    """Remove a person's individual limit for one period. **Platform admin only.**

    Added with the 2026-09-07 ruling that removed self-service writes: an
    individual row must remain removable — deleting it is how a person FALLS BACK
    to the applicable default (or to uncapped where none exists). Without this
    route no party could undo an individual grant.

    **204 whether or not a row existed** — the requested outcome ("no individual
    row for this person/period") holds either way, and a 404 on the already-absent
    case would make a retried delete look like a failure.

    Raises:
        HTTPException:
            ``401`` when unauthenticated;
            ``403`` for any caller who is not a platform admin;
            ``422`` for a malformed or unlinked anchor;
            ``503`` when the delete fails — never a silent success, which would
            leave a rule in force that the admin believes they removed.
    """
    AccessControl(db).require_platform_admin(current_user)

    try:
        resolved_anchor = await resolve_person_anchor(db, person_anchor)
        await db.execute(
            delete(PersonBudgetConfig).where(
                PersonBudgetConfig.person_anchor == resolved_anchor,
                PersonBudgetConfig.period_type == period_type,
            )
        )
        await db.commit()
    except _INFRASTRUCTURE_FAULTS as exc:
        raise _unavailable(exc) from exc

    logger.info("person_cap_deleted scope=platform_admin period=%s", period_type)


@router.get("/budget/person-default/{scope}", response_model=PersonDefaultResponse)
async def get_person_default(
    scope: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_type: PersonCapPeriod = "monthly",
) -> PersonDefaultResponse:
    """Read one scope's default person limit. **Platform admin only.**

    Reads the rule authored FOR this exact scope — not the rule that would apply to
    a member of it. A team with no team-scoped default returns ``uncapped`` here even
    when a platform default governs everybody in it, because this surface is for
    managing rules and conflating the two would make a deletion look like a no-op.
    The resolved-per-person view is ``GET /me/budget/person-cap`` (for oneself) and
    the admin UI of #4691.

    Args:
        scope: ``platform``, ``org:<org_id>``, or ``team:<org_id>:<team_id>``.
        period_type: Which calendar period's rule to read.

    Raises:
        HTTPException:
            ``401`` when unauthenticated;
            ``403`` for any caller who is not a platform admin;
            ``422`` for a malformed scope or a non-calendar ``period_type``;
            ``503`` when the table is unreadable — never a ``200`` reading as "no
            default set".
    """
    # Authority first, before the scope is even parsed: a non-admin must not be able
    # to use the 422-vs-200 difference to probe which orgs and teams exist.
    AccessControl(db).require_platform_admin(current_user)

    scope_type, scope_id_org, scope_id_team = _parse_scope(scope)

    try:
        row = await _read_default_row(db, scope_type, scope_id_org, scope_id_team, period_type)
    except _INFRASTRUCTURE_FAULTS as exc:
        raise _default_unavailable(exc) from exc

    return _compose_default(row) if row is not None else _uncapped_default(scope_type, scope_id_org, scope_id_team, period_type)


@router.put("/budget/person-default/{scope}", response_model=PersonDefaultResponse)
async def put_person_default(
    scope: str,
    request: PersonDefaultRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_type: PersonCapPeriod = "monthly",
) -> PersonDefaultResponse:
    """Set one scope's default person limit. **Platform admin only.**

    This is the "$1,000/month each, unless we say otherwise" write. It governs every
    current AND future member of the scope who has no individual row and no
    tighter-scoped default — which is what distinguishes it from writing the same
    number into every person's row, where every future joiner would start unlimited.

    **It takes effect within the enforcement gate's TTL, not instantly** (currently
    60s, ``_PERSON_CAPS_EXISTENCE_TTL_SECONDS``). On an install whose person-limit
    tables were both empty, the first rule authored here has to wait for the
    process-local existence cache to expire before the person layer starts consulting
    them at all. That is the documented cost of keeping the hot path free of a query
    per request, and it is bounded — see ``docs/budget-ratelimit.md``.

    Written ``hard``: a governance default that silently did not enforce would be an
    inert cap (#4511) at platform scale, and there is no pre-enforcement generation
    of these rows to keep a promise to (unlike the C3-era individual ``soft`` rows).

    Idempotent: re-authoring replaces the amount in place, keeping the row's ``id``
    and ``created_at`` (when this rule was first set, not when the number last
    changed) while re-stamping ``authored_by_user_id``.

    Args:
        scope: ``platform``, ``org:<org_id>``, or ``team:<org_id>:<team_id>``.
        period_type: Which calendar period this rule governs.

    Raises:
        HTTPException:
            ``401`` when unauthenticated;
            ``403`` for any caller who is not a platform admin;
            ``422`` for a malformed scope, a non-positive/over-precise amount, or a
            non-calendar ``period_type`` — nothing is written in any of those cases;
            ``503`` when the write fails, never a ``200`` that would leave an admin
            believing a population is bounded when it is not.
    """
    AccessControl(db).require_platform_admin(current_user)

    scope_type, scope_id_org, scope_id_team = _parse_scope(scope)

    try:
        # Inside the try: this reads the DB, and its faults map to the 503 like
        # every other read (review pattern from #4644). Existence, not shape
        # (review fix on #4696): a rule aimed at a mistyped/foreign/deleted scope
        # would store cleanly and govern nobody — the #4511 class on the
        # governance surface itself.
        await _require_scope_exists(db, scope_type, scope_id_org, scope_id_team)
        row = await _upsert_default(
            db,
            scope_type=scope_type,
            scope_id_org=scope_id_org,
            scope_id_team=scope_id_team,
            period_type=period_type,
            amount=request.budget_amount_usd,
            # Canonical id for the audit column — the admin's token id is a Cognito
            # sub on the JWT path (the #4647 contract; see put_my_person_cap).
            authored_by_user_id=await resolve_canonical_user_id(db, current_user.user_id),
        )
    except _INFRASTRUCTURE_FAULTS as exc:
        raise _default_unavailable(exc) from exc

    # The scope is logged because this write governs a POPULATION: "a default was
    # authored" without saying which scope is unactionable in an incident.
    logger.info("person_default_authored scope_type=%s period=%s", scope_type, period_type)
    return _compose_default(row)


@router.delete("/budget/person-default/{scope}", status_code=204)
async def delete_person_default(
    scope: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_type: PersonCapPeriod = "monthly",
) -> None:
    """Remove one scope's default person limit. **Platform admin only.**

    A DELETE, not a ``PUT`` of ``0`` — ``0`` is a real ceiling of zero dollars
    applied to everybody in the scope, which is why the request model rejects it.

    **Removing a rule does not make its members unlimited** if a broader rung still
    covers them: deleting a team default leaves that team governed by their org's
    default, or the platform's. Only deleting the last applicable rule restores
    "unlimited", and only for people with no individual row.

    ``204`` whether or not a rule existed: the outcome the caller asked for holds
    either way, and a ``404`` for the already-absent case would make a retried delete
    look like a failure.

    Raises:
        HTTPException:
            ``401`` when unauthenticated;
            ``403`` for any caller who is not a platform admin;
            ``422`` for a malformed scope or a non-calendar ``period_type``;
            ``503`` when the delete fails — never a silent success, which would leave
            a rule in force that the admin believes they removed.
    """
    AccessControl(db).require_platform_admin(current_user)

    scope_type, scope_id_org, scope_id_team = _parse_scope(scope)

    try:
        await db.execute(
            delete(PersonBudgetDefault).where(
                *_default_row_predicate(scope_type, scope_id_org, scope_id_team),
                PersonBudgetDefault.period_type == period_type,
            )
        )
        await db.commit()
    except _INFRASTRUCTURE_FAULTS as exc:
        raise _default_unavailable(exc) from exc

    logger.info("person_default_deleted scope_type=%s period=%s", scope_type, period_type)
