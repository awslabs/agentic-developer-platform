"""Monthly person spend and the Organization → Team → User budget hierarchy.

Money comes from the same attributed ledger keys and policy resolver as enforcement.
Global directory/spend reads require platform authority before reading any rows.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.budget import BudgetUsage, PersonBudgetConfig, PersonBudgetDefault
from src.shared.models.organization import Organization, Team, User
from src.shared.schemas.auth import TokenContext

from .person_ledger import (
    PersonLimit,
    resolve_applicable_person_limits,
    resolve_member_partitions,
    resolve_person_default_limits,
    resolve_person_identity,
    resolve_person_subs,
    resolve_person_team_keys,
)
from .schemas import CAP_PLACES, SPEND_PLACES, format_money

router = APIRouter(tags=["Budget & Spend"])
DB = Annotated[AsyncSession, Depends(get_db)]
Caller = Annotated[TokenContext, Depends(get_current_user)]


class Amounts(BaseModel):
    direct_usd: str
    cloud_usd: str
    total_usd: str


class DailySpend(Amounts):
    date: str
    in_progress: bool


class MonthlySpend(BaseModel):
    month: str
    resets_at: str
    as_of: str
    totals: Amounts
    days: list[DailySpend]
    daily_complete: bool


def amounts(direct: Decimal, cloud: Decimal) -> Amounts:
    return Amounts(
        direct_usd=format_money(direct, SPEND_PLACES),
        cloud_usd=format_money(cloud, SPEND_PLACES),
        total_usd=format_money(direct + cloud, SPEND_PLACES),
    )


async def monthly_spend(db: AsyncSession, user_ids: list[str], org_ids: list[str], *, include_days: bool = True) -> MonthlySpend:
    now = datetime.now(UTC)
    today = now.date()
    start = today.replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    subs = await resolve_person_subs(db, user_ids)
    # A single statement reads the monthly and daily aggregates at one DB snapshot.
    # Never include organization rollups or the shared hosted-worker's user rows.
    periods = ["monthly", "daily"] if include_days else ["monthly"]
    rows = (
        (
            await db.execute(
                select(BudgetUsage).where(
                    BudgetUsage.org_id.in_(org_ids),
                    or_(
                        and_(BudgetUsage.entity_type == "root_user", BudgetUsage.entity_id.in_(user_ids)),
                        and_(BudgetUsage.entity_type == "user", BudgetUsage.entity_id.in_(subs)),
                    ),
                    BudgetUsage.period_type.in_(periods),
                    BudgetUsage.period_start >= start,
                    BudgetUsage.period_start <= today,
                )
            )
        )
        .scalars()
        .all()
    )
    direct = cloud = Decimal(0)
    days = {start + timedelta(days=n): [Decimal(0), Decimal(0)] for n in range(today.day)}
    for row in rows:
        component = 0 if row.entity_type == "user" else 1
        if row.period_type == "monthly" and row.period_start == start:
            if component == 0:
                direct += row.total_cost_usd
            else:
                cloud += row.total_cost_usd
        elif row.period_type == "daily":
            days[row.period_start][component] += row.total_cost_usd
    complete = include_days and sum((v[0] for v in days.values()), Decimal(0)) == direct and sum((v[1] for v in days.values()), Decimal(0)) == cloud
    return MonthlySpend(
        month=start.isoformat(),
        resets_at=end.isoformat(),
        as_of=now.isoformat(),
        totals=amounts(direct, cloud),
        daily_complete=complete,
        days=[DailySpend(date=day.isoformat(), in_progress=day == today, **amounts(*days[day]).model_dump()) for day in sorted(days, reverse=True)]
        if complete
        else [],
    )


@router.get("/me/budget/monthly-spend", response_model=MonthlySpend)
async def get_monthly_spend(current_user: Caller, db: DB):
    user = await db.scalar(select(User).where(or_(User.cognito_sub == current_user.user_id, User.id == current_user.user_id)).limit(1))
    if not user or user.user_kind != "human" or current_user.account_type != "human":
        raise HTTPException(422, "Personal spend is unavailable for this identity")
    _, ids = await resolve_person_identity(db, user.id)
    return await monthly_spend(db, ids, await resolve_member_partitions(db, ids, None))


class LimitView(BaseModel):
    amount_usd: str | None = None
    source: str | None = None
    source_label: str | None = None
    enforcement_mode: str | None = None


def limit_view(limit: PersonLimit | None, labels: dict[str, str] | None = None) -> LimitView:
    if limit is None:
        return LimitView()
    return LimitView(
        amount_usd=format_money(limit.amount, CAP_PLACES),
        source=limit.source,
        source_label=(labels or {}).get(limit.scope_label, "Individual budget" if limit.source in {"own", "admin"} else limit.scope_label),
        enforcement_mode=limit.enforcement_mode,
    )


class BudgetNode(BaseModel):
    key: str
    kind: str
    name: str
    org_id: str | None = None
    team_id: str | None = None
    person_anchor: str | None = None
    configured_usd: str | None = None
    effective: LimitView
    fallback: LimitView
    children: list["BudgetNode"] = []


async def directory(db: AsyncSession):
    """Deduplicate canonical people before pagination, never paginate memberships."""
    users = (await db.execute(select(User).where(User.user_kind == "human").order_by(User.name, User.email, User.id))).scalars().all()
    people = {}
    seen = set()
    for user in users:
        if user.id in seen:
            continue
        anchor, ids = await resolve_person_identity(db, user.id)
        seen.update(ids)
        if anchor not in people:
            people[anchor] = (user, ids)
    return people


@router.get("/budget/hierarchy", response_model=BudgetNode)
async def get_hierarchy(current_user: Caller, db: DB):
    AccessControl(db).require_platform_admin(current_user)
    defaults = (await db.execute(select(PersonBudgetDefault).where(PersonBudgetDefault.period_type == "monthly"))).scalars().all()
    configs = {
        row.person_anchor: row
        for row in (await db.execute(select(PersonBudgetConfig).where(PersonBudgetConfig.period_type == "monthly"))).scalars().all()
    }
    rules = {(r.scope_type, r.scope_id_org or None, r.scope_id_team or None): r for r in defaults}

    def scope_node(kind, name, parent=None, org=None, team=None):
        row = rules.get((kind, org, team))
        fallback = parent.effective if parent else LimitView()
        own = (
            LimitView(
                amount_usd=format_money(row.budget_amount_usd, CAP_PLACES),
                source=f"{kind}_default",
                source_label=f"{name} · {'Organization' if kind == 'org' else kind.title()}",
                enforcement_mode=row.enforcement_mode,
            )
            if row
            else None
        )
        return BudgetNode(
            key=f"{kind}:{org or ''}:{team or ''}",
            kind=kind,
            name=name,
            org_id=org,
            team_id=team,
            configured_usd=own.amount_usd if own else None,
            effective=own or fallback,
            fallback=fallback,
        )

    root = scope_node("platform", "Platform default")
    orgs = {}
    teams = {}
    for org in (await db.execute(select(Organization).order_by(Organization.name))).scalars().all():
        node = scope_node("org", org.name, root, org.id)
        root.children.append(node)
        orgs[org.id] = node
    for team in (await db.execute(select(Team).order_by(Team.name))).scalars().all():
        if team.org_id in orgs:
            node = scope_node("team", team.name, orgs[team.org_id], team.org_id, team.id)
            orgs[team.org_id].children.append(node)
            teams[(team.org_id, team.id)] = node
    labels = {f"org default for {org_id}": f"Organization · {node.name}" for org_id, node in orgs.items()}
    labels.update({f"team default for {team_id} in {org_id}": f"Team · {node.name}" for (org_id, team_id), node in teams.items()})
    for anchor, (user, ids) in (await directory(db)).items():
        partitions = await resolve_member_partitions(db, ids, None)
        team_keys = await resolve_person_team_keys(db, ids)
        limits = await resolve_applicable_person_limits(db, person_anchor=anchor, org_ids=partitions, team_keys=team_keys)
        inherited = await resolve_person_default_limits(db, partitions, team_keys)
        config = configs.get(anchor)
        node = BudgetNode(
            key=anchor,
            kind="user",
            name=user.name or user.email,
            person_anchor=anchor,
            configured_usd=format_money(config.budget_amount_usd, CAP_PLACES) if config else None,
            effective=limit_view(limits.get("monthly"), labels),
            fallback=limit_view(inherited.get("monthly"), labels),
        )
        parents = [teams[key] for key in team_keys if key in teams]
        assigned_orgs = {parent.org_id for parent in parents}
        parents += [orgs[org] for org in partitions if org in orgs and org not in assigned_orgs]
        for parent in parents or [root]:
            parent.children.append(node)
    return root


class PersonSpend(BaseModel):
    person_anchor: str
    name: str
    email: str
    spend: Amounts
    budget: LimitView


class PeopleSpend(BaseModel):
    items: list[PersonSpend]
    total: int
    page: int
    page_size: int


@router.get("/budget/people-spend", response_model=PeopleSpend)
async def get_people_spend(
    current_user: Caller, db: DB, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), search: str = Query("", max_length=200)
):
    AccessControl(db).require_platform_admin(current_user)
    people = [
        (anchor, user, ids)
        for anchor, (user, ids) in (await directory(db)).items()
        if search.casefold() in f"{user.name or ''} {user.email}".casefold()
    ]
    labels = {f"org default for {org.id}": f"Organization · {org.name}" for org in (await db.scalars(select(Organization))).all()}
    labels.update({f"team default for {team.id} in {team.org_id}": f"Team · {team.name}" for team in (await db.scalars(select(Team))).all()})
    items = []
    for anchor, user, ids in people[(page - 1) * page_size : page * page_size]:
        partitions = await resolve_member_partitions(db, ids, None)
        limits = await resolve_applicable_person_limits(
            db, person_anchor=anchor, org_ids=partitions, team_keys=await resolve_person_team_keys(db, ids)
        )
        spend = await monthly_spend(db, ids, partitions, include_days=False)
        items.append(
            PersonSpend(
                person_anchor=anchor,
                name=user.name or user.email,
                email=user.email,
                spend=spend.totals,
                budget=limit_view(limits.get("monthly"), labels),
            )
        )
    return PeopleSpend(items=items, total=len(people), page=page, page_size=page_size)
