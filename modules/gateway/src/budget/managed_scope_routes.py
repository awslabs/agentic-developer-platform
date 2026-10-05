"""Managed-scope budget read API — Issue #4401 (U-4 of EPIC #4324).

``GET /budget/scope/{entity_type}/{entity_id}`` answers, for a team lead,
department admin, org owner or platform operator, the question operators
literally cannot answer today: "who is spending, against what cap, driven by
which runs" — for any entity **within their managed scope**, and for nothing
outside it.

**This is the load-bearing security surface of the EPIC.** It is the only unit
that accepts a *target* other than the caller, so it is the only one carrying
cross-tenant risk: an endpoint that returns another person's spend is a
reportable data-leak incident. Out-of-scope denial is therefore the acceptance
gate (FR-4.2), and every branch that produces a ``403`` is covered by a test.

**Why this is a new module and not a route in ``routes.py``** (NFR-1, a hard
constraint): ``src/budget/routes.py`` reads ``entity_type``/``entity_id`` from the
request with **no scope check at all** and is the subject of open IDOR #4384.
Adding a scoped route beside an unscoped one invites the next reviewer to assume
the file is safe, so the two live apart. U-1's ``me_routes.py`` took the same
decision for the same reason; this module is its permission-gated counterpart,
deliberately separate from it as well because the two answer different questions:
``/me/*`` accepts no target and needs no authorisation beyond authentication,
while every route here is meaningless without one.

**Read-only** (NFR-2). Nothing here writes ``budget_configs`` or ``budget_usage``,
and no enforcement behaviour changes.

The composition helpers, spend/cap reads and run-list logic are **imported from
``me_routes``, never reimplemented** — that module documents them as pure and
reusable for exactly this unit. A second implementation is how an operator's view
and a user's own view of the same entity drift apart (the read/write asymmetry
class of #4322); reuse makes disagreement structurally impossible rather than a
thing to test for.

Four properties of the authorisation, each of which is a test:

1. ``entity_type`` is an **allow-list** (``user|team|department|org|root_user``).
   RUN and CHAIN are lifetime-scoped, not calendar-period entities, and reach
   period logic that cannot handle them — a ``500`` for what is really a bad
   request. Enforced by a ``Literal`` at the HTTP boundary, so FastAPI answers
   ``422`` before any handler code runs.
2. Authority is resolved **server-side from ``tenant_memberships``**, via
   ``AccessControl``, and **never** from a token claim (FR-4.4). A forged or
   stale ``custom:role`` claim grants nothing.
3. A falsy target id is **explicitly denied**, never skipped (FR-4.3).
4. Denial is a **single, identical ``403`` carrying no entity metadata** — not
   even whether the target exists (FR-4.2).

Points 3 and 4 are the two that are easy to get subtly wrong, so each has its own
section below.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.activity.cost_service import get_cost_by_run_ids
from src.activity.routes import get_activity_service
from src.activity.service import ActivityService
from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError, InvalidScopeError
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity.workspaces import primary_team_for_workspace, workspace_user
from src.shared.models.budget import BudgetUsage
from src.shared.models.organization import Department, Team, User
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

from .enforcement_service import _INFRASTRUCTURE_FAULTS
from .me_routes import (
    _compose_line,
    _figure_from_cost_row,
    _period_bounds_as_instants,
    _principal_kind_for,
    _read_cap,
    _read_settled_spend,
    _resolve_period_bounds,
    _run_attribution,
    _subtotal_figure,
)
from .schemas import (
    BudgetPeriod,
    BudgetRunItem,
    CostFigure,
    ManagedScopeBudgetResponse,
    ManagedScopeRunsResponse,
    ScopeRollupRow,
)

logger = logging.getLogger("bedrockgateway.budget")

# No `/api` prefix: CloudFront strips the first `/api` segment before the origin,
# so a router mounting under `/api/...` is unreachable through the dashboard
# (issue #4330, guarded by tests/test_route_prefix_convention.py). The browser
# calls `/api/budget/scope/...`; this router serves `/budget/scope/...`.
#
# The prefix is `/budget`, NOT `/budgets` — `/budgets` is the unscoped router of
# #4384, and sharing its prefix would put a scope-checked route and an
# unscope-checked one in the same namespace, where the next reader cannot tell
# which is which from the path alone.
router = APIRouter(prefix="/budget/scope", tags=["budget"])


# The entity types a managed-scope read may name. RUN and CHAIN are deliberately
# ABSENT: their caps are lifetime-scoped, `get_period_start_end` raises for them,
# and an unguarded request would surface that as a 500 for what is really a bad
# request. AGENT and SERVICE_ACCOUNT are absent too — neither is a calendar-period
# entity a managed-scope screen reports on today, and an allow-list must grant
# only what is asked for (the issue names exactly these five).
#
# A `Literal` rather than a runtime check, so FastAPI rejects an unlisted value
# with its own 422 at the HTTP boundary, before any handler code — and before any
# authorisation code — runs. That ordering matters: a type this route cannot
# handle must not reach the scope logic at all.
ScopeEntityType = Literal["user", "team", "department", "org", "root_user"]

# Which allow-listed targets are CONTAINERS of members, and which are single
# principals. A container gets per-member `rollup` rows; a single principal gets
# none, because a rollup of one row describing the target itself is the target's
# own line restated.
_CONTAINER_ENTITY_TYPES: frozenset[str] = frozenset(
    {
        EntityType.TEAM.value,
        EntityType.DEPARTMENT.value,
        EntityType.ORGANIZATION.value,
    }
)


# ---------------------------------------------------------------------------
# Denial — one shape, no metadata
# ---------------------------------------------------------------------------
#
# Every refusal on this router raises THIS, with this exact message, whatever the
# reason: out of scope, target in another tenant, target does not exist, target id
# blank, caller holds no permission. That uniformity is the security property
# (FR-4.2).
#
# A 403 that differs for an existing versus a non-existent target is an
# ENUMERATION ORACLE: an attacker who cannot read a colleague's spend can still
# harvest the org chart by diffing responses, learning who exists, which teams
# there are, and which ids are real. So there is no 404 anywhere on this router —
# "no such team" and "not your team" are indistinguishable on the wire, on
# purpose. `detail` names no entity type, no id, no tenant and no role.
#
# It is a module-level constant rather than an inline string at each raise site so
# that the four properties above cannot drift apart as sites are added: a new
# denial path that wants a *different* message is a change to this contract and
# has to come here to make it.
_DENIAL_DETAIL = "Not authorized to read budget data for the requested scope."


def _deny(reason: str, *, caller_org: str, entity_type: str) -> HTTPException:
    """Build the single, uniform ``403``.

    The *reason* is logged, never returned. Operators need to distinguish "blank
    id" from "cross-tenant attempt" when reading logs — and a cross-tenant attempt
    is a security event worth alerting on — but the caller must learn nothing from
    the difference, which is what makes the response body identical in every case.

    ``entity_type`` is safe to log: it came from the allow-listed ``Literal``, so
    it is one of five known constants and cannot carry caller-supplied content.
    The target **id** is deliberately NOT logged — a blocked cross-tenant probe
    would otherwise write another tenant's identifiers into this tenant's log
    stream.

    Args:
        reason: Short machine-greppable cause, for the log line only.
        caller_org: The caller's own authenticated org, for log attribution.
        entity_type: The allow-listed target type, for log attribution.

    Returns:
        The ``HTTPException`` to raise. Returned rather than raised so each call
        site reads as ``raise _deny(...)`` and no path can accidentally build a
        denial without raising it.
    """
    logger.warning(
        "managed_scope_budget_denied reason=%s caller_org=%s entity_type=%s",
        reason,
        caller_org,
        entity_type,
    )
    return HTTPException(status_code=403, detail=_DENIAL_DETAIL)


def _require_target_id(entity_id: str, *, caller_org: str, entity_type: str) -> str:
    """Deny a falsy or blank target id **explicitly** (FR-4.3).

    This is the highest-severity variant of the whole unit, because it does not
    look like an attack: the request is well-formed and the parameter is simply
    empty, so a check written as ``if target_id and target_id != allowed`` treats
    it as "nothing to compare" and falls through to **allow**.

    That is not hypothetical — it is the shape at ``src/activity/routes.py:280,287``
    (and inside ``AccessControl.check_permission`` itself, whose scope comparisons
    are both guarded by ``if target_... and allowed_...``). A blank id reaching
    those predicates skips the mismatch check entirely. This function is why no
    blank id ever reaches them from this router: the deny happens here, first,
    unconditionally.

    ``.strip()`` matters as much as the emptiness test. FastAPI's routing rejects a
    genuinely absent segment (``/budget/scope/user/``) with a ``404`` before any
    handler runs, so the values that actually arrive here and are *effectively*
    blank are the encoded ones — ``%20`` is a one-space id that is truthy in
    Python, would be compared as a real value, and matches no ledger row. Treating
    whitespace as present would put a meaningless id into a scope comparison.

    Returns:
        The stripped id, so every downstream read uses the same normalised value
        that was authorised — authorising one string and querying another is its
        own class of bug.

    Raises:
        HTTPException: ``403``, the uniform denial.
    """
    normalized = (entity_id or "").strip()
    if not normalized:
        raise _deny("falsy_target_id", caller_org=caller_org, entity_type=entity_type)
    return normalized


# ---------------------------------------------------------------------------
# Scope resolution — server-side, from the database
# ---------------------------------------------------------------------------
#
# `AccessControl.check_permission` is called for the permission gate and the ORG
# boundary, but it is deliberately NOT the whole check, because on its own it
# cannot see the target:
#
#   * It compares the caller's authority against a `target_org_id` the CALLER's
#     request supplied. Nothing in it knows which org actually owns the entity
#     being read, so passing the caller's own org would make the comparison
#     trivially true for any target. The owning org therefore has to be resolved
#     from the DATABASE first, and that resolved value is what gets passed in.
#   * Its department branch is `if target_dept_id and allowed_dept_id and ...`,
#     and `get_user_role` returns `(role, tenant_id, None)` — `allowed_dept_id` is
#     ALWAYS None. So that branch can never fire, and a dept_admin reaching
#     another department inside their own org would pass every check it performs.
#     The department boundary is therefore enforced here (see `_check_department_scope`).
#
# Both are cases of the same rule: authority comes from the server, and so must the
# facts it is compared against.


async def _resolve_target_org(
    db: AsyncSession,
    entity_type: str,
    entity_id: str,
) -> str | None:
    """Resolve which org owns the target, from the database.

    This is the value the scope check compares against, so it must come from a
    stored row and never from the request. A caller who could supply it would be
    authorising themselves.

    Returns:
        The owning ``org_id``, or ``None`` when the target cannot be located.
        ``None`` means the target is **not readable by anyone through this
        route** and the caller is denied — the same denial as out-of-scope, so the
        two are indistinguishable (no existence oracle).

        For an ``org`` target the id IS the org, so it is returned as-is; the
        caller's authority over it is then checked exactly as for any other
        target, which is what stops a cross-tenant org read.

    Note the ``user`` lookup matches ``cognito_sub`` **or** ``id``: a ``user``
    ledger row is keyed by Cognito sub while a ``root_user`` row is keyed by
    canonical ``users.id`` (#4300), and both entity types are allow-listed here.
    Matching only one form would deny a legitimate operator read of a real
    principal — and, worse, would do so with the same 403 as a genuine
    cross-tenant attempt, making the bug invisible.
    """
    if entity_type == EntityType.ORGANIZATION.value:
        return entity_id

    if entity_type in (EntityType.USER.value, EntityType.ROOT_USER.value):
        # A `service:`-qualified root principal is not a `users` row and cannot be
        # resolved to an owning org this way. It is denied rather than guessed at:
        # stripping the qualifier and matching the remainder would authorise a
        # read against a row whose ownership was never established.
        return await db.scalar(select(User.org_id).where((User.cognito_sub == entity_id) | (User.id == entity_id)).limit(1))

    if entity_type == EntityType.TEAM.value:
        return await db.scalar(select(Team.org_id).where(Team.id == entity_id).limit(1))

    if entity_type == EntityType.DEPARTMENT.value:
        return await db.scalar(select(Department.org_id).where(Department.id == entity_id).limit(1))

    # Unreachable: `ScopeEntityType` admits exactly the five branches above, and
    # FastAPI rejects anything else with a 422 before this runs. Present so that
    # ADDING a member to that Literal without teaching this function about it
    # fails CLOSED (deny) rather than open.
    return None


async def _check_department_scope(
    db: AsyncSession,
    access: AccessControl,
    context: TokenContext,
    entity_type: str,
    entity_id: str,
    *,
    target_org: str,
) -> bool:
    """Confine a ``dept_admin`` to their own department.

    Needed because ``check_permission``'s department branch is unreachable (see
    the section comment above): it requires a truthy ``allowed_dept_id``, and
    ``get_user_role`` never supplies one. Without this, a ``dept_admin`` could read
    any department, team or member in their org — negative test 3 of the issue.

    The caller's department is resolved from the current workspace account and
    authoritative primary team. A token department is not sufficient authority.

    Returns:
        ``True`` when the target is inside the caller's department, ``False`` when
        it is outside and must be denied.
    """
    role, _, _ = await access.get_user_role(context)
    if role.value != "dept_admin":
        # Not a dept-scoped caller: the org boundary already checked by
        # `check_permission` is the whole of their limit. A platform admin has no
        # department at all, and an org admin legitimately spans every department
        # in their own org.
        return True

    caller = await workspace_user(db, context.user_id, target_org, username=context.cognito_username)
    caller_team = await primary_team_for_workspace(db, caller, target_org) if caller else None
    caller_dept = caller_team.department_id if caller_team else None
    if not caller_dept:
        # A dept_admin with no department is scoped to nothing. Denied rather than
        # treated as unrestricted — this is the falsy-skip failure applied to the
        # caller's side of the comparison, and defaulting it to "allow" would make
        # an unscoped dept_admin the most privileged role on this router.
        return False

    if entity_type == EntityType.DEPARTMENT.value:
        return entity_id == caller_dept

    if entity_type == EntityType.ORGANIZATION.value:
        # An org target aggregates every department, which is strictly wider than
        # the caller's own. Reading it would leak the other departments' spend in
        # aggregate even though each one individually is out of scope.
        return False

    if entity_type == EntityType.TEAM.value:
        team_dept = await db.scalar(select(Team.department_id).where(Team.id == entity_id, Team.org_id == target_org).limit(1))
        return bool(team_dept) and team_dept == caller_dept

    # A user/root_user target: their department must be the caller's. `users` has
    # no department column, so it is resolved through the user's team — the same
    # team->department edge the check above uses.
    user_team = await db.scalar(
        select(User.team_id).where((User.cognito_sub == entity_id) | (User.id == entity_id), User.org_id == target_org).limit(1)
    )
    if not user_team:
        # No team, so no department can be established. Denied: an unplaceable
        # principal must not fall through to allow.
        return False
    user_dept = await db.scalar(select(Team.department_id).where(Team.id == user_team, Team.org_id == target_org).limit(1))
    return bool(user_dept) and user_dept == caller_dept


async def _authorize_scope(
    db: AsyncSession,
    access: AccessControl,
    context: TokenContext,
    entity_type: str,
    entity_id: str,
) -> tuple[str, str]:
    """Authorise a managed-scope read, or raise the uniform ``403``.

    The single entry point for every route on this router, in this order:

    1. **Falsy target id → deny.** First, before anything can treat "empty" as
       "nothing to compare" (FR-4.3).
    2. **Resolve the owning org from the database** — not from the request.
    3. **Permission + org boundary** via ``AccessControl.check_permission``, whose
       authority comes from ``tenant_memberships`` (FR-4.4). ``BUDGET_READ`` is in
       ``_ORG_SCOPED_PERMISSIONS``, so a caller with no org membership is rejected
       there rather than served empty data.
    4. **Department boundary**, which ``check_permission`` cannot enforce.

    Every failure raises the same ``403`` with the same body. Note step 2 runs
    before step 3 by necessity — the check needs the resolved org — and an
    unresolvable target is denied at that point, so a caller cannot use step 2 to
    probe for existence: they get the identical denial either way.

    Returns:
        ``(target_org_id, normalised_entity_id)``. The normalised id is returned so
        the ledger reads use exactly the string that was authorised.

    Raises:
        HTTPException: ``403`` for any authorisation failure; ``503`` when the
            authorisation stores themselves are unreadable — **never** a fallthrough
            to allow, and never a ``200``.
    """
    caller_org = context.org_id
    normalized_id = _require_target_id(entity_id, caller_org=caller_org, entity_type=entity_type)

    try:
        target_org = await _resolve_target_org(db, entity_type, normalized_id)
    except _INFRASTRUCTURE_FAULTS as exc:
        # The store that establishes WHO OWNS the target is unreadable, so scope
        # cannot be established. 503, never a guess: failing open here would serve
        # another tenant's spend during a database blip, and failing to a 403 would
        # mislabel an outage as a permission problem.
        logger.error("Failed to resolve managed-scope target ownership; refusing the read", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Budget figures are temporarily unavailable. This is a backend failure, not a report of zero spend.",
        ) from exc

    if not target_org:
        # Unlocatable target. Same 403 as out-of-scope, deliberately: a 404 here
        # would confirm non-existence and turn this route into an org-chart
        # enumeration oracle.
        raise _deny("target_not_resolvable", caller_org=caller_org, entity_type=entity_type)

    try:
        await access.check_permission(context, Permission.BUDGET_READ, target_org_id=target_org)
    except (AccessDeniedError, InvalidScopeError) as exc:
        # Both are 403-carrying exceptions with descriptive messages naming roles,
        # permissions and scopes. They are caught and REPLACED with the uniform
        # denial rather than allowed to propagate: their detail distinguishes
        # "wrong role" from "wrong org", which tells a prober which of the two they
        # got right.
        raise _deny(f"permission_denied:{type(exc).__name__}", caller_org=caller_org, entity_type=entity_type) from exc

    try:
        in_department = await _check_department_scope(db, access, context, entity_type, normalized_id, target_org=target_org)
    except (*_INFRASTRUCTURE_FAULTS, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Budget authorization is temporarily unavailable") from exc
    if not in_department:
        raise _deny("department_out_of_scope", caller_org=caller_org, entity_type=entity_type)

    return target_org, normalized_id


async def _container_member_ledger_ids(
    db: AsyncSession,
    org_id: str,
    entity_type: str,
    entity_id: str,
) -> set[str]:
    """Resolve which ledger ids belong to a ``team``/``department`` container.

    ``budget_usage`` is keyed by principal id alone — it carries no team or
    department edge — so the container's membership has to come from the ``users``
    table: ``User.team_id`` for a team, and the team→department edge for a
    department (the same edge ``_check_department_scope`` walks).

    Both id namespaces are returned per member: a ``user`` ledger row is keyed by
    Cognito sub while a ``root_user`` row is keyed by canonical ``users.id``
    (#4300), and a member's spend can land in either.

    A ``service:``-qualified root principal has no ``users`` row, so it cannot be
    placed in a team or department and is therefore ABSENT from these sets — it
    appears only in an ``org`` rollup, whose membership needs no placement. That is
    the fail-closed direction: an unplaceable principal must not surface in a
    container it was never proven to belong to.
    """
    stmt = select(User.cognito_sub, User.id).where(User.org_id == org_id)
    if entity_type == EntityType.TEAM.value:
        stmt = stmt.where(User.team_id == entity_id)
    else:  # department: members are the users of the department's teams
        stmt = stmt.where(User.team_id.in_(select(Team.id).where(Team.department_id == entity_id, Team.org_id == org_id)))
    rows = (await db.execute(stmt)).all()
    return {value for row in rows for value in row if value}


async def _read_rollup_rows(
    db: AsyncSession,
    org_id: str,
    entity_type: str,
    entity_id: str,
    period_type: PeriodType,
    period_start: date,
) -> list[ScopeRollupRow]:
    """Build per-member rollup rows for a container target.

    Only for ``team``/``department``/``org`` targets — a single principal has no
    members. The rows come from the ``budget_usage`` rows that already exist for
    the period, filtered to the two per-principal ledgers (``user`` and
    ``root_user``): those are where a person's spend actually lands, so they are
    what a member table must show.

    **Every row carries ``principal_kind``** (FR-2.5), derived by U-2's
    ``_principal_kind_for`` from the ``service:`` id qualifier — so ``ci-bot`` is
    reported as ``service`` and a member table cannot render an unattended trigger
    as a colleague.

    The read is filtered on ``(org_id, entity_type, period_type, period_start)``
    and enumerates rows within it. This is deliberately **not** a ``SUM``: each row
    is one principal's own ledger figure, passed through the same
    ``_compose_line`` helper as every other line on this surface, so nothing here
    aggregates across entity types the way ``get_organization_budget_overview``
    does (#4328).

    **Membership scoping** (T9d/T9e): a ``team`` or ``department`` target's rollup
    is additionally confined to **that container's members**, resolved through
    ``users`` (see ``_container_member_ledger_ids``). Without it, a dept_admin who
    legitimately reads their own department would receive every principal in the
    org — other departments' per-member spend included — which is exactly the scope
    ``_check_department_scope`` exists to deny. The authorisation gate says the
    caller may read the *container*; this filter is what makes the response contain
    only the container.
    """
    if entity_type not in _CONTAINER_ENTITY_TYPES:
        return []

    rollup_query = select(BudgetUsage).where(
        BudgetUsage.org_id == org_id,
        BudgetUsage.entity_type.in_((EntityType.USER.value, EntityType.ROOT_USER.value)),
        BudgetUsage.period_type == period_type.value,
        BudgetUsage.period_start == period_start,
    )
    if entity_type != EntityType.ORGANIZATION.value:
        member_ids = await _container_member_ledger_ids(db, org_id, entity_type, entity_id)
        if not member_ids:
            # A container with no resolvable members has nothing to roll up. An
            # empty list, not the org-wide fallthrough — "no members" must never
            # widen into "everyone".
            return []
        rollup_query = rollup_query.where(BudgetUsage.entity_id.in_(member_ids))

    usage_rows = (await db.execute(rollup_query.order_by(BudgetUsage.entity_type, BudgetUsage.entity_id))).scalars().all()

    rows: list[ScopeRollupRow] = []
    for usage in usage_rows:
        member_type = EntityType(usage.entity_type)
        cap_row = await _read_cap(db, org_id, member_type, usage.entity_id, period_type)
        line = _compose_line(member_type, usage.entity_id, cap_row, usage.total_cost_usd or Decimal("0"))
        rows.append(
            ScopeRollupRow(
                entity_type=usage.entity_type,
                entity_id=usage.entity_id,
                principal_kind=_principal_kind_for(member_type, usage.entity_id),
                line=line,
            )
        )
    return rows


@router.get("/{entity_type}/{entity_id}", response_model=ManagedScopeBudgetResponse)
async def get_managed_scope_budget(
    entity_type: ScopeEntityType,
    entity_id: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_type: Annotated[
        Literal["daily", "weekly", "monthly"], Query(description="Calendar period to report. Run/chain caps are not calendar periods.")
    ] = "monthly",
) -> ManagedScopeBudgetResponse:
    """Return a target entity's cap, settled spend and headroom, for an operator.

    **Authorisation is the point of this endpoint** — see ``_authorize_scope``.
    Permission and scope are resolved server-side from ``tenant_memberships``
    (FR-4.4); a falsy target id is explicitly denied (FR-4.3); and every denial is
    the same ``403`` with no entity metadata, not even whether the target exists
    (FR-4.2).

    The figures come from the **same helpers** as ``GET /me/budget``, so an
    operator's view of a user and that user's own view cannot disagree. Container
    targets additionally carry per-member ``rollup`` rows, each with
    ``principal_kind`` so a service account is not rendered as a person (FR-2.5).

    Returns:
        ``200`` with the target's figures for the requested period.

    Raises:
        HTTPException:
            ``401`` when unauthenticated (from ``get_current_user``);
            ``403`` for any authorisation failure — uniform, metadata-free;
            ``422`` for an ``entity_type`` outside the allow-list or a non-calendar
            ``period_type`` (RUN/CHAIN are not calendar entities);
            ``503`` when the ledger is unreadable — **never** a ``200`` with
            zeroes, which during an outage would read as "this team has spent
            nothing".
    """
    resolved_period, period_start, period_end = _resolve_period_bounds(period_type)

    access = AccessControl(db)
    target_org, target_id = await _authorize_scope(db, access, current_user, entity_type, entity_id)

    resolved_entity_type = EntityType(entity_type)

    try:
        cap_row = await _read_cap(db, target_org, resolved_entity_type, target_id, resolved_period)
        spend = await _read_settled_spend(db, target_org, resolved_entity_type, target_id, resolved_period, period_start)
        rollup = await _read_rollup_rows(db, target_org, entity_type, target_id, resolved_period, period_start)
    except _INFRASTRUCTURE_FAULTS as exc:
        # Same rule as U-1: a failed ledger read must not render as "$0 spent".
        logger.error("Failed to read managed-scope budget; returning 503 rather than a zeroed budget", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Budget figures are temporarily unavailable. This is a backend failure, not a report of zero spend.",
        ) from exc

    line = _compose_line(resolved_entity_type, target_id, cap_row, spend)

    return ManagedScopeBudgetResponse(
        period=BudgetPeriod(
            period_type=resolved_period.value,
            period_start=period_start,
            period_end=period_end,
            resets_in_days=max(0, (period_end - date.today()).days),
        ),
        entity_type=entity_type,
        entity_id=target_id,
        line=line,
        # Selected, never summed (FR-2.3). An uncapped line cannot bind (contract
        # rule 6), so `binding` is null rather than this line when no cap governs
        # the target — reporting an uncapped line as binding would render a null
        # headroom as the headline.
        binding=line if line.cap_status == "capped" else None,
        rollup=rollup,
    )


@router.get("/{entity_type}/{entity_id}/runs", response_model=ManagedScopeRunsResponse)
async def get_managed_scope_budget_runs(
    entity_type: ScopeEntityType,
    entity_id: str,
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
) -> ManagedScopeRunsResponse:
    """List the agent runs that contributed to a target's spend in one period.

    U-3's drill-down for an operator-named target. Authorised by exactly the same
    ``_authorize_scope`` as the endpoint above — the run list is *more* sensitive
    than the totals, since it names individual runs and personas, so it gets the
    same gate rather than a lighter one.

    **Container targets carry no runs.** A team/department/org is not a lineage
    partition: ``webhook-events`` is partitioned by principal, so there is no
    query that returns "this team's runs". Rather than return an empty list — which
    would read as "this team ran nothing" — the response carries an ``unknown``
    subtotal, the same honesty rule U-3 applies when a caller's identity does not
    resolve.

    Cost is three-valued exactly as in U-3: a run with no ``usage_logs`` row is
    ``unknown``, never ``$0.00``, because back-fill is asynchronous and that is the
    ordinary state of a recent run.

    Returns:
        ``200`` with the target's runs for the requested period. Also ``200``,
        with every cost ``unknown``, when the cost store cannot be read.

    Raises:
        HTTPException:
            ``401`` when unauthenticated;
            ``403`` for any authorisation failure — uniform, metadata-free;
            ``400`` for a malformed ``cursor``;
            ``422`` for an out-of-allow-list ``entity_type`` or non-calendar
            ``period_type``;
            ``503`` when the lineage store is unreadable — never a ``200`` with an
            empty list, which would say "nothing ran".
    """
    resolved_period, resolved_start, resolved_end = _resolve_period_bounds(period_type, period_start)

    access = AccessControl(db)
    target_org, target_id = await _authorize_scope(db, access, current_user, entity_type, entity_id)

    period = BudgetPeriod(
        period_type=resolved_period.value,
        period_start=resolved_start,
        period_end=resolved_end,
        resets_in_days=max(0, (resolved_end - date.today()).days),
    )

    if entity_type not in (EntityType.USER.value, EntityType.ROOT_USER.value):
        # A container target. No lineage partition exists for it, so there is no
        # set of runs to report. `unknown`, not an empty list with a $0 subtotal —
        # "we cannot enumerate this" and "nothing ran" are different claims, and
        # conflating them is the EPIC's headline failure.
        logger.info(
            "managed_scope_runs_container_target entity_type=%s caller_org=%s",
            entity_type,
            current_user.org_id,
        )
        return ManagedScopeRunsResponse(
            items=[],
            subtotal=CostFigure(status="unknown", reason="lineage_unavailable", partial=True),
            total_run_count=0,
            next_cursor=None,
            period=period,
            entity_type=entity_type,
            entity_id=target_id,
        )

    # The lineage partition key is the canonical `users.id`. A `user` target is
    # keyed in the LEDGER by Cognito sub (#4300), which is a different namespace,
    # so it is resolved here rather than passed straight through — querying the
    # partition with a Cognito sub matches nothing and would report a real spender
    # as having run nothing.
    lineage_user_id = target_id
    if entity_type == EntityType.USER.value:
        try:
            resolved_user = await workspace_user(db, target_id, target_org)
            resolved = resolved_user.id if resolved_user else None
        except _INFRASTRUCTURE_FAULTS as exc:
            logger.error("Failed to resolve the managed-scope target's canonical id; returning 503", exc_info=True)
            raise HTTPException(
                status_code=503,
                detail="Run history is temporarily unavailable. This is a backend failure, not a report of zero runs.",
            ) from exc
        if not resolved:
            # No canonical id, so no partition to read. `unknown` rather than an
            # empty list, for the same reason as the container case above.
            return ManagedScopeRunsResponse(
                items=[],
                subtotal=CostFigure(status="unknown", reason="lineage_unavailable", partial=True),
                total_run_count=0,
                next_cursor=None,
                period=period,
                entity_type=entity_type,
                entity_id=target_id,
            )
        lineage_user_id = resolved

    since, until = _period_bounds_as_instants(resolved_start, resolved_end)

    try:
        lineage = activity.query_by_user(
            user_id=lineage_user_id, tenant_id=target_org, page_size=page_size, last_key=cursor, since=since, until=until
        )
    except ValueError as exc:
        # A malformed cursor is a bad request, not a server error (the
        # `activity/routes.py` precedent, mirrored by U-3).
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except _INFRASTRUCTURE_FAULTS as exc:
        logger.error("Failed to read managed-scope run lineage; returning 503 rather than an empty run list", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Run history is temporarily unavailable. This is a backend failure, not a report of zero runs.",
        ) from exc

    # Join key is `invocation_id` (the DynamoDB `event_id`), which equals
    # `usage_logs.agent_run_id` — NOT `InvocationItem.run_id`, which carries the
    # KEDA job name and matches no usage row, reporting every run as free.
    run_ids = [item.invocation_id for item in lineage.items if item.invocation_id]

    absent_reason = "no_usage_rows"
    cost_map: dict[str, dict] = {}
    if run_ids:
        try:
            cost_map = await get_cost_by_run_ids(db, run_ids)
        except Exception:
            # Graceful degradation, as in U-3: the run list is true without cost,
            # so this stays a 200 with every figure `unknown`. The reason
            # distinguishes "ledger had no rows" from "ledger unreadable".
            logger.warning("Failed to enrich managed-scope runs with cost; reporting unknown cost rather than failing the request", exc_info=True)
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

    return ManagedScopeRunsResponse(
        items=items,
        subtotal=_subtotal_figure(items),
        total_run_count=len(items),
        next_cursor=lineage.last_key,
        period=period,
        entity_type=entity_type,
        entity_id=target_id,
    )
