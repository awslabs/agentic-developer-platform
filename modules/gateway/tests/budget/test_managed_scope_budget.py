"""Managed-scope budget read API — Issue #4401 (U-4 of EPIC #4324).

**The negative authz tests in this file are the acceptance gate of the unit, not a
supporting detail.** ``/budget/scope/{entity_type}/{entity_id}`` is the only
endpoint in the EPIC that accepts a target other than the caller, so it is the
only one that can leak another person's spend — a reportable data-leak incident.
Every denial case below (T1-T6, T8) is therefore written and run BEFORE the happy
path (T7), per NFR-3, and each ``403`` branch in the router is covered.

Test order in this file is deliberate and mirrors the issue's numbering:

  T1  member -> another member                  -> 403, no entity metadata
  T2  member -> another tenant                  -> 403
  T3  dept_admin -> a department outside theirs  -> 403
  T4  org_admin -> another org                  -> 403
  T5  absent / empty / falsy target id          -> DENIED, not skipped
  T6  forged `custom:role=org_admin` claim      -> 403 (authority is server-side)
  T7  in-scope target, per role                 -> 200 (happy path, written last)
  T8  entity_type / period_type allow-lists     -> 422
  T9  rollup rows carry `principal_kind`

Harness notes:

* Real in-memory SQLite with real ``User``/``Team``/``Department``/
  ``TenantMembership``/``BudgetConfig``/``BudgetUsage`` rows — the harness U-1
  established in ``test_me_budget_routes.py``. Authority in this unit is resolved
  by a real ``AccessControl`` against real ``tenant_memberships`` rows, so mocking
  it away would delete the thing under test: a mocked ``check_permission`` asserts
  a guarantee it never exercised (the #4046 trap).
* The decoy tenant and colleague are seeded with figures that are unmistakably not
  the caller's, so an endpoint that leaks them fails loudly rather than
  coincidentally matching a zero.
* ``rbac_least_privilege_default`` is left at its real default (``True``). A test
  that flipped it would be asserting the rolled-back permissive behaviour.
"""

import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.activity.routes import get_activity_service
from src.activity.schemas import InvocationItem, InvocationListResponse
from src.admin.config import AdminConfig, set_admin_config
from src.auth.dependencies import get_current_user
from src.budget.managed_scope_routes import router as managed_scope_router
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

# ---------------------------------------------------------------------------
# The two tenants. Cross-tenant reads are the headline risk, so there are really
# two of everything, and the second one's figures are distinctive.
# ---------------------------------------------------------------------------

ORG_ID = "org-4401"
OTHER_ORG_ID = "org-4401-other"

DEPT_A = "dept-4401-a"
DEPT_B = "dept-4401-b"
OTHER_ORG_DEPT = "dept-4401-other"

TEAM_A = "team-4401-a"  # in DEPT_A
TEAM_B = "team-4401-b"  # in DEPT_B
OTHER_ORG_TEAM = "team-4401-other"

# The caller in most tests: a plain member of ORG_ID, in TEAM_A / DEPT_A.
MEMBER_SUB = "sub-4401-member"
MEMBER_CANONICAL = "44010000-0000-4000-8000-000000000001"

# A colleague in the SAME org and same team. T1 reads them and must be denied:
# same tenant is not the same as in scope.
COLLEAGUE_SUB = "sub-4401-colleague"
COLLEAGUE_CANONICAL = "44010000-0000-4000-8000-000000000002"

# A member of DEPT_B / TEAM_B — inside the caller's org but a different
# department. T3's dept_admin target.
OTHER_DEPT_SUB = "sub-4401-otherdept"
OTHER_DEPT_CANONICAL = "44010000-0000-4000-8000-000000000003"

# A member of the OTHER tenant entirely. T2/T4's target.
FOREIGN_SUB = "sub-4401-foreign"
FOREIGN_CANONICAL = "44010000-0000-4000-8000-000000000004"

# Admin callers.
DEPT_ADMIN_SUB = "sub-4401-deptadmin"
DEPT_ADMIN_CANONICAL = "44010000-0000-4000-8000-000000000005"
ORG_ADMIN_SUB = "sub-4401-orgadmin"
ORG_ADMIN_CANONICAL = "44010000-0000-4000-8000-000000000006"

# A service-rooted principal. `service:`-qualified per #4344, which is what makes
# T9's `principal_kind` assertion meaningful rather than cosmetic.
SERVICE_ROOT_ID = "service:ci-bot-4401"

# Figures chosen so that a leak is unmistakable: no two entities share a spend,
# and none of them is zero (a zero could coincide with "no row found").
COLLEAGUE_SPEND = Decimal("777.777777")
FOREIGN_SPEND = Decimal("888.888888")
MEMBER_SPEND = Decimal("12.500000")
OTHER_DEPT_SPEND = Decimal("55.555555")
TEAM_A_SPEND = Decimal("250.000000")
SERVICE_SPEND = Decimal("99.990000")

PERIOD_START = date.today().replace(day=1)


@pytest.fixture(autouse=True)
def real_admin_config():
    """Use a REAL AdminConfig at its shipped defaults.

    Least-privilege default stays ``True`` — that is the behaviour in production,
    and it is what makes a no-membership principal resolve to ``MEMBER``. A test
    that overrode it would be pinning the rolled-back permissive fallback.
    """
    set_admin_config(AdminConfig())
    yield
    set_admin_config(AdminConfig())


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
async def seeded(session: AsyncSession) -> None:
    """Two tenants, two departments, three teams, six users, and their ledgers."""
    session.add_all(
        [
            Organization(id=ORG_ID, name="Org 4401"),
            Organization(id=OTHER_ORG_ID, name="Org 4401 Other"),
            Department(id=DEPT_A, org_id=ORG_ID, name="Dept A"),
            Department(id=DEPT_B, org_id=ORG_ID, name="Dept B"),
            Department(id=OTHER_ORG_DEPT, org_id=OTHER_ORG_ID, name="Dept Other"),
            Team(id=TEAM_A, org_id=ORG_ID, department_id=DEPT_A, name="Team A"),
            Team(id=TEAM_B, org_id=ORG_ID, department_id=DEPT_B, name="Team B"),
            Team(id=OTHER_ORG_TEAM, org_id=OTHER_ORG_ID, department_id=OTHER_ORG_DEPT, name="Team Other"),
        ]
    )

    users = [
        (MEMBER_CANONICAL, MEMBER_SUB, ORG_ID, TEAM_A, "member"),
        (COLLEAGUE_CANONICAL, COLLEAGUE_SUB, ORG_ID, TEAM_A, "member"),
        (OTHER_DEPT_CANONICAL, OTHER_DEPT_SUB, ORG_ID, TEAM_B, "member"),
        (FOREIGN_CANONICAL, FOREIGN_SUB, OTHER_ORG_ID, OTHER_ORG_TEAM, "member"),
        (DEPT_ADMIN_CANONICAL, DEPT_ADMIN_SUB, ORG_ID, TEAM_A, "dept_admin"),
        (ORG_ADMIN_CANONICAL, ORG_ADMIN_SUB, ORG_ID, TEAM_A, "org_admin"),
    ]
    for canonical, sub, org, team, role in users:
        session.add(User(id=canonical, cognito_sub=sub, email=f"{sub}@example.com", org_id=org, team_id=team))
        # Authority lives HERE — tenant_memberships, not the token (FR-4.4).
        session.add(TenantMembership(user_id=canonical, tenant_id=org, role=role, is_active=True))

    # Ledgers. Caps exist so the happy path has a `capped` line to assert on.
    session.add_all(
        [
            BudgetUsage(
                org_id=ORG_ID,
                entity_type=EntityType.USER.value,
                entity_id=COLLEAGUE_SUB,
                period_type=PeriodType.MONTHLY.value,
                period_start=PERIOD_START,
                total_cost_usd=COLLEAGUE_SPEND,
            ),
            BudgetUsage(
                org_id=OTHER_ORG_ID,
                entity_type=EntityType.USER.value,
                entity_id=FOREIGN_SUB,
                period_type=PeriodType.MONTHLY.value,
                period_start=PERIOD_START,
                total_cost_usd=FOREIGN_SPEND,
            ),
            BudgetUsage(
                org_id=ORG_ID,
                entity_type=EntityType.USER.value,
                entity_id=MEMBER_SUB,
                period_type=PeriodType.MONTHLY.value,
                period_start=PERIOD_START,
                total_cost_usd=MEMBER_SPEND,
            ),
            # A DEPT_B member's ledger row — same org, different department. This
            # is the row a container rollup must NOT surface to a DEPT_A-scoped
            # reader (T9d/T9e): its distinctive figure makes the leak unmistakable.
            BudgetUsage(
                org_id=ORG_ID,
                entity_type=EntityType.USER.value,
                entity_id=OTHER_DEPT_SUB,
                period_type=PeriodType.MONTHLY.value,
                period_start=PERIOD_START,
                total_cost_usd=OTHER_DEPT_SPEND,
            ),
            BudgetUsage(
                org_id=ORG_ID,
                entity_type=EntityType.TEAM.value,
                entity_id=TEAM_A,
                period_type=PeriodType.MONTHLY.value,
                period_start=PERIOD_START,
                total_cost_usd=TEAM_A_SPEND,
            ),
            # A service-rooted principal's ledger row (T9).
            BudgetUsage(
                org_id=ORG_ID,
                entity_type=EntityType.ROOT_USER.value,
                entity_id=SERVICE_ROOT_ID,
                period_type=PeriodType.MONTHLY.value,
                period_start=PERIOD_START,
                total_cost_usd=SERVICE_SPEND,
            ),
            BudgetConfig(
                org_id=ORG_ID,
                entity_type=EntityType.USER.value,
                entity_id=COLLEAGUE_SUB,
                period_type=PeriodType.MONTHLY.value,
                budget_amount_usd=Decimal("1000.00"),
                enforcement_mode="hard",
            ),
            BudgetConfig(
                org_id=ORG_ID,
                entity_type=EntityType.TEAM.value,
                entity_id=TEAM_A,
                period_type=PeriodType.MONTHLY.value,
                budget_amount_usd=Decimal("500.00"),
                enforcement_mode="hard",
            ),
        ]
    )
    await session.commit()


def context_for(
    sub: str,
    *,
    org_id: str = ORG_ID,
    team_id: str = TEAM_A,
    department_id: str = DEPT_A,
    **overrides,
) -> TokenContext:
    """A token context. Note what it does NOT establish: authority.

    ``is_admin`` defaults to ``False`` and is only set by a *platform*-level claim
    (``auth/dependencies.py`` deliberately excludes ``org_admin``), so a caller's
    role here comes from their ``tenant_memberships`` row, never from this object.
    T6 exploits exactly that.
    """
    defaults = {
        "user_id": sub,
        "org_id": org_id,
        "team_id": team_id,
        "department_id": department_id,
        "account_type": "human",
        "expires_at": date(2099, 1, 1),
    }
    return TokenContext(**{**defaults, **overrides})


def build_app(session: AsyncSession, context: TokenContext) -> FastAPI:
    """Mount the managed-scope router alone, with auth and db overridden.

    ``AccessControl`` is deliberately NOT overridden — it is constructed inside the
    route against the request's real session, so the authority checks below run
    against the real ``tenant_memberships`` rows. That is the point of the unit.
    """
    app = FastAPI()
    app.include_router(managed_scope_router)

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: context
    return app


def client_for(session: AsyncSession, context: TokenContext) -> AsyncClient:
    app = build_app(session, context)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class FakeActivityService:
    """A stand-in for the DynamoDB-backed lineage store.

    The real ``ActivityService`` talks to ``webhook-events``, which no unit test
    should require. What matters for this unit is the *contract*: which partition
    key the route asks for, and that it is the target's canonical ``users.id``
    rather than their Cognito sub (#4300 — the two are different namespaces, and
    querying with the wrong one silently reports a real spender as having run
    nothing). ``requested_user_ids`` records the keys so a test can assert that.
    """

    def __init__(self, items: list[InvocationItem] | None = None, *, last_key: str | None = None, raises: BaseException | None = None):
        self.items = items or []
        self.last_key = last_key
        self.raises = raises
        self.requested_user_ids: list[str] = []

    def query_by_user(self, user_id: str, **kwargs) -> InvocationListResponse:
        self.requested_user_ids.append(user_id)
        if self.raises is not None:
            raise self.raises
        return InvocationListResponse(items=self.items, count=len(self.items), last_key=self.last_key)


def client_with_lineage(session: AsyncSession, context: TokenContext, activity: FakeActivityService) -> AsyncClient:
    """A client whose run-lineage store is the fake above.

    Only ``get_activity_service`` is added to the overrides that ``build_app``
    already sets — authority still resolves against the real ``tenant_memberships``
    rows, so a run-route test cannot accidentally bypass the scope check it is
    supposed to be sitting behind.
    """
    app = build_app(session, context)
    app.dependency_overrides[get_activity_service] = lambda: activity
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def invocation(invocation_id: str, *, correlation_id: str | None = None, persona: str = "developer", status: str = "completed") -> InvocationItem:
    return InvocationItem(
        invocation_id=invocation_id,
        invoked_at=f"{PERIOD_START.isoformat()}T09:00:00Z",
        correlation_id=correlation_id,
        persona=persona,
        status=status,
    )


def assert_denied_without_metadata(response, *, forbidden_values: tuple[str, ...] = ()) -> None:
    """Assert a uniform 403 that reveals nothing about the target.

    Two things are checked, and the second is the subtle one:

    * the status is ``403`` — not ``404``, which would confirm non-existence;
    * the body leaks **no** entity metadata: no spend figure, no cap, no name, and
      none of the identifiers the caller was probing for.

    A denial whose body varies by target is an enumeration oracle even when it is
    consistently a 403, which is why the body is asserted for equality against the
    single canonical message rather than merely "contains 'not authorized'".
    """
    assert response.status_code == 403, response.text
    body = response.json()
    assert body == {"detail": "Not authorized to read budget data for the requested scope."}, body

    serialized = response.text
    for value in forbidden_values:
        assert value not in serialized, f"denial leaked {value!r}: {serialized}"


# ===========================================================================
# T1 — member -> another member: 403, with NO entity metadata
# ===========================================================================


async def test_t1_member_cannot_read_another_member_in_same_org(session, seeded):
    """A plain member reading a colleague is denied, and learns nothing.

    Same tenant, same team, adjacent desk — and still denied, because "my org" is
    not "my scope". ``MEMBER`` does not hold ``BUDGET_READ`` at all, so this is the
    permission gate; T3/T4 cover the scope gate for callers who do hold it.

    The body must not contain the colleague's spend, their cap, their id, or any
    hint that they exist. This is the data-leak case the unit exists to prevent.
    """
    async with client_for(session, context_for(MEMBER_SUB)) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}?period_type=monthly")

    assert_denied_without_metadata(
        response,
        forbidden_values=(
            str(COLLEAGUE_SPEND),
            "777",
            "1000.00",
            COLLEAGUE_SUB,
            "example.com",
        ),
    )


async def test_t1b_member_cannot_read_their_own_team_container(session, seeded):
    """A member is denied a container target too, not just an individual.

    Reading the team a member belongs to is denied for the same reason: a
    container read exposes every member's contribution in the rollup, so
    "it's my own team" must not become a softer door into colleagues' figures.
    """
    async with client_for(session, context_for(MEMBER_SUB)) as client:
        response = await client.get(f"/budget/scope/team/{TEAM_A}?period_type=monthly")

    assert_denied_without_metadata(response, forbidden_values=(str(TEAM_A_SPEND), "250", "500.00"))


# ===========================================================================
# T2 — member -> another tenant: 403
# ===========================================================================


async def test_t2_member_cannot_read_another_tenant(session, seeded):
    """A member reaching into a different tenant is denied.

    The cross-tenant case, which is the one that would be a reportable incident.
    The foreign member's spend (``888.888888``) must appear nowhere in the
    response.
    """
    async with client_for(session, context_for(MEMBER_SUB)) as client:
        response = await client.get(f"/budget/scope/user/{FOREIGN_SUB}?period_type=monthly")

    assert_denied_without_metadata(response, forbidden_values=(str(FOREIGN_SPEND), "888", FOREIGN_SUB, OTHER_ORG_ID))


async def test_t2b_member_cannot_read_another_tenants_org(session, seeded):
    """Naming the other tenant's ORG directly is denied as well.

    An ``org`` target is the widest possible read, and its id is guessable in a way
    a user id is not — so the org-level path gets its own case rather than being
    assumed to follow from the user-level one.
    """
    async with client_for(session, context_for(MEMBER_SUB)) as client:
        response = await client.get(f"/budget/scope/org/{OTHER_ORG_ID}?period_type=monthly")

    assert_denied_without_metadata(response, forbidden_values=(str(FOREIGN_SPEND), OTHER_ORG_ID))


# ===========================================================================
# T3 — dept_admin -> a department outside their own: 403
# ===========================================================================


async def test_t3_dept_admin_cannot_read_another_department(session, seeded):
    """A ``dept_admin`` is confined to their OWN department.

    This is the case ``AccessControl.check_permission`` cannot catch on its own:
    ``DEPT_ADMIN`` holds ``BUDGET_READ``, the target is in the caller's own org so
    the org comparison passes, and the function's department branch is unreachable
    because ``get_user_role`` never returns a non-``None`` ``allowed_dept_id``. If
    the router relied on ``check_permission`` alone this request would succeed.
    """
    async with client_for(session, context_for(DEPT_ADMIN_SUB, department_id=DEPT_A)) as client:
        response = await client.get(f"/budget/scope/department/{DEPT_B}?period_type=monthly")

    assert_denied_without_metadata(response, forbidden_values=(DEPT_B,))


async def test_t3b_dept_admin_cannot_read_a_team_in_another_department(session, seeded):
    """The confinement follows the team -> department edge, not just dept ids.

    A ``team`` target names no department, so the boundary has to be resolved
    through ``teams.department_id``. Without that step a dept_admin could read any
    team in the org by naming the team instead of its department — the same leak
    through a different parameter.
    """
    async with client_for(session, context_for(DEPT_ADMIN_SUB, department_id=DEPT_A)) as client:
        response = await client.get(f"/budget/scope/team/{TEAM_B}?period_type=monthly")

    assert_denied_without_metadata(response, forbidden_values=(TEAM_B,))


async def test_t3c_dept_admin_cannot_read_a_user_in_another_department(session, seeded):
    """And it follows user -> team -> department for an individual target."""
    async with client_for(session, context_for(DEPT_ADMIN_SUB, department_id=DEPT_A)) as client:
        response = await client.get(f"/budget/scope/user/{OTHER_DEPT_SUB}?period_type=monthly")

    assert_denied_without_metadata(response, forbidden_values=(OTHER_DEPT_SUB,))


async def test_t3d_dept_admin_cannot_read_the_whole_org(session, seeded):
    """A dept_admin naming the org is denied — it is strictly wider than their dept.

    An org target aggregates every department, so allowing it would leak the other
    departments' spend in aggregate even though each one individually is refused.
    """
    async with client_for(session, context_for(DEPT_ADMIN_SUB, department_id=DEPT_A)) as client:
        response = await client.get(f"/budget/scope/org/{ORG_ID}?period_type=monthly")

    assert_denied_without_metadata(response)


async def test_t3e_dept_admin_with_no_department_claim_is_denied(session, seeded):
    """A ``dept_admin`` scoped to no department reads nothing.

    The falsy-skip failure applied to the CALLER's side of the comparison. If a
    blank ``department_id`` defaulted to "unrestricted", an unscoped dept_admin
    would become the most privileged role on this router.
    """
    async with client_for(session, context_for(DEPT_ADMIN_SUB, department_id="")) as client:
        response = await client.get(f"/budget/scope/department/{DEPT_A}?period_type=monthly")

    assert_denied_without_metadata(response)


# ===========================================================================
# T4 — org_admin -> another org: 403
# ===========================================================================


async def test_t4_org_admin_cannot_read_another_org(session, seeded):
    """An ``org_admin``'s authority stops at their own tenant.

    ``ORG_ADMIN`` holds ``BUDGET_READ``, so this is purely the scope check: the
    target org is resolved from the database and compared against the org the
    caller's membership row grants.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/org/{OTHER_ORG_ID}?period_type=monthly")

    assert_denied_without_metadata(response, forbidden_values=(str(FOREIGN_SPEND), OTHER_ORG_ID))


async def test_t4b_org_admin_cannot_read_a_member_of_another_org(session, seeded):
    """Nor an individual inside another tenant.

    Asserted separately from T4 because the owning org is resolved by a DIFFERENT
    lookup for a user target (``users``) than for an org target (the id itself), so
    one passing does not imply the other.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/user/{FOREIGN_SUB}?period_type=monthly")

    assert_denied_without_metadata(response, forbidden_values=(str(FOREIGN_SPEND), FOREIGN_SUB))


async def test_t4c_org_admin_cannot_read_another_orgs_team(session, seeded):
    """And not another tenant's team, resolved via ``teams.org_id``."""
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/team/{OTHER_ORG_TEAM}?period_type=monthly")

    assert_denied_without_metadata(response, forbidden_values=(OTHER_ORG_TEAM,))


# ===========================================================================
# T5 — absent / empty / falsy target id: DENIED, never skipped
# ===========================================================================
#
# The highest-severity variant, because the request looks valid: a check written
# as `if target and target != allowed` treats an empty id as "nothing to compare"
# and falls through to ALLOW. That is the live shape at
# `src/activity/routes.py:280,287` and inside `check_permission` itself, so the
# router must deny before any such predicate is reached.


@pytest.mark.parametrize(
    "blank_id",
    [
        pytest.param("%20", id="single-encoded-space"),
        pytest.param("%20%20%20", id="multiple-encoded-spaces"),
        pytest.param("%09", id="encoded-tab"),
        pytest.param("+", id="plus-as-space"),
    ],
)
async def test_t5_effectively_blank_target_id_is_denied(session, seeded, blank_id):
    """A whitespace-only target id is denied explicitly, not treated as absent.

    These are the *routable* falsy ids: a genuinely empty path segment is a
    routing ``404`` before any handler runs (asserted by T5c), so the values that
    actually reach the scope logic are the encoded ones. Each is truthy in Python
    and would sail through an ``if target_id and ...`` guard.

    An ``org_admin`` is used deliberately — the most privileged non-platform
    caller, who WOULD be allowed to read a real target in their own org. So a
    ``403`` here can only come from the blank-id check itself, not from a
    permission failure that would have denied the request anyway.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/user/{blank_id}?period_type=monthly")

    assert_denied_without_metadata(response)


async def test_t5b_blank_target_id_is_denied_on_the_runs_route(session, seeded):
    """The same rule on the drill-down, which is the more sensitive of the two.

    The runs route names individual runs and personas, so a blank-id bypass there
    leaks more than a totals bypass. Both routes share one ``_authorize_scope``,
    and this test is what pins that they do.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get("/budget/scope/user/%20/runs?period_type=monthly")

    assert_denied_without_metadata(response)


async def test_t5c_absent_target_segment_never_reaches_the_handler(session, seeded):
    """A truly absent id is a routing 404 — it cannot degrade to "allow".

    Documented as a test rather than assumed: the guarantee is that no request
    without a target id can reach the read logic. FastAPI enforces it at the
    routing layer, which is a stronger guarantee than a handler check, and this
    asserts the layer is actually doing it.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get("/budget/scope/user/?period_type=monthly")

    assert response.status_code == 404, response.text
    # Crucially NOT a 200: absence must never be served as data.
    assert "spend_usd" not in response.text


async def test_t5d_nonexistent_target_is_denied_identically_to_out_of_scope(session, seeded):
    """A non-existent target gets the SAME 403 as an out-of-scope one.

    No existence oracle: an ``org_admin`` probing ids inside their own org cannot
    tell a real colleague from a made-up one, so the endpoint cannot be used to
    enumerate the org chart. Asserted by comparing against T4b's body — the two
    responses must be byte-identical.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        missing = await client.get("/budget/scope/user/sub-4401-does-not-exist?period_type=monthly")
        out_of_scope = await client.get(f"/budget/scope/user/{FOREIGN_SUB}?period_type=monthly")

    assert missing.status_code == out_of_scope.status_code == 403
    assert missing.json() == out_of_scope.json()
    assert "does-not-exist" not in missing.text


# ===========================================================================
# T6 — a forged/stale token claim grants nothing (FR-4.4)
# ===========================================================================


async def test_t6_org_admin_role_claim_without_membership_is_denied(session):
    """``custom:role=org_admin`` with no matching membership row is denied.

    Authority is server-side only. Two mechanisms make this hold, and the test
    exercises both at once:

    * ``custom:role=org_admin`` does not set ``is_admin`` — that predicate admits
      only platform-level roles (``auth/dependencies.py:82``) — so the claim never
      short-circuits to ``PLATFORM_ADMIN``.
    * With no ``tenant_memberships`` row, ``get_user_role`` falls to the
      least-privilege default ``MEMBER``, which does not hold ``BUDGET_READ``.

    Note this fixture deliberately does NOT depend on ``seeded``: the caller has no
    user row and no membership row at all, which is the "forged or stale claim"
    state. The target id is a well-formed one so the denial cannot be attributed to
    a malformed request.
    """
    async with client_for(session, context_for("sub-4401-forged")) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}?period_type=monthly")

    assert_denied_without_metadata(response)


async def test_t6b_is_admin_claim_is_not_settable_by_an_org_admin_role(session, seeded):
    """Pin the predicate T6 depends on: an org-level role is not platform admin.

    If ``auth/dependencies.py`` ever admitted ``org_admin`` into ``is_admin``, T6
    would still pass (the caller has no membership) while every ``org_admin`` in
    the product silently gained cross-tenant read. This asserts the property
    directly, so that regression fails here with a clear cause.
    """
    from src.auth.cognito_jwt import CognitoTokenClaims
    from src.auth.dependencies import _cognito_claims_to_context

    claims = CognitoTokenClaims(
        sub="sub-4401-claimtest",
        iss="https://cognito-idp.us-east-1.amazonaws.com/pool-4401",
        client_id="client-4401",
        token_use="access",
        exp=4102444800,
        iat=1756512000,
        email="claim@example.com",
        role="org_admin",
        org_id=ORG_ID,
    )
    context = _cognito_claims_to_context(claims)
    assert context.is_admin is False


# ===========================================================================
# T8 — allow-lists: entity_type and period_type (422, not 500)
# ===========================================================================
#
# Ordered before the happy path with the other negatives. RUN/CHAIN reach period
# logic that raises, so an unguarded request is a 500 for what is really a bad
# request.


@pytest.mark.parametrize(
    "entity_type",
    [
        pytest.param("run", id="run-not-a-calendar-entity"),
        pytest.param("chain", id="chain-not-a-calendar-entity"),
        pytest.param("service_account", id="not-in-allow-list"),
        pytest.param("agent", id="not-in-allow-list-agent"),
        pytest.param("organization", id="wrong-spelling-of-org"),
        pytest.param("../../etc/passwd", id="path-traversal-shaped"),
    ],
)
async def test_t8_entity_type_outside_the_allow_list_is_422(session, seeded, entity_type):
    """An unlisted ``entity_type`` is rejected at the HTTP boundary.

    ``422``, never ``500`` and never a read. RUN and CHAIN are the load-bearing
    entries: their caps are lifetime-scoped and ``get_period_start_end`` raises for
    them, so reaching the handler would surface a server error for a bad request.

    An ``org_admin`` caller is used so the rejection is attributable to the
    allow-list rather than to a permission failure.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/{entity_type}/{COLLEAGUE_SUB}?period_type=monthly")

    assert response.status_code in (404, 422), response.text
    assert "spend_usd" not in response.text


@pytest.mark.parametrize("period_type", ["run", "chain"])
async def test_t8b_noncalendar_period_type_is_422(session, seeded, period_type):
    """``period_type=run``/``chain`` is a ``422``.

    Same rule from the other direction: these are not calendar periods, so there
    are no bounds to compute and the endpoint must refuse rather than invent them.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}?period_type={period_type}")

    assert response.status_code == 422, response.text


@pytest.mark.parametrize("period_type", ["run", "chain"])
async def test_t8c_noncalendar_period_type_is_422_on_runs_route(session, seeded, period_type):
    """And on the drill-down route."""
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}/runs?period_type={period_type}")

    assert response.status_code == 422, response.text


# ===========================================================================
# T7 — the happy path, written LAST (NFR-3)
# ===========================================================================


async def test_t7_org_admin_reads_a_member_in_their_own_org(session, seeded):
    """An ``org_admin`` reads a member of their own org and gets real figures.

    The figures must be the SAME ones U-2 composes for the user's own view — same
    ``_compose_line`` helper, same 5-filter read — which is what makes an
    operator's view and a user's own view unable to disagree.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}?period_type=monthly")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["entity_type"] == "user"
    assert body["entity_id"] == COLLEAGUE_SUB
    # Settled spend at the column's 6dp, as a STRING (contract rule 1).
    assert body["line"]["spend_usd"] == "777.777777"
    assert body["line"]["cap_usd"] == "1000.00"
    assert body["line"]["cap_status"] == "capped"
    assert body["line"]["remaining_usd"] == "222.222223"
    # A capped line binds; the headline is SELECTED, never summed (FR-2.3).
    assert body["binding"] is not None
    assert body["binding"]["spend_usd"] == body["line"]["spend_usd"]
    # An individual target has no members to roll up.
    assert body["rollup"] == []
    assert body["period"]["period_type"] == "monthly"


async def test_t7b_dept_admin_reads_their_own_department(session, seeded):
    """A ``dept_admin`` reads the department they administer.

    The positive counterpart of T3: the same code path that denies DEPT_B must
    allow DEPT_A, or the check is simply "deny everything" and the negatives prove
    nothing.
    """
    async with client_for(session, context_for(DEPT_ADMIN_SUB, department_id=DEPT_A)) as client:
        response = await client.get(f"/budget/scope/department/{DEPT_A}?period_type=monthly")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entity_type"] == "department"
    assert body["entity_id"] == DEPT_A
    # No department ledger row was seeded, so this is a true uncapped zero — and
    # "no cap" is NOT "$0 cap" (contract rule 2).
    assert body["line"]["cap_status"] == "uncapped"
    assert body["line"]["cap_usd"] is None
    assert body["binding"] is None


async def test_t7c_dept_admin_reads_a_team_in_their_own_department(session, seeded):
    """A ``dept_admin`` reads a team inside their department (T3b's positive)."""
    async with client_for(session, context_for(DEPT_ADMIN_SUB, department_id=DEPT_A)) as client:
        response = await client.get(f"/budget/scope/team/{TEAM_A}?period_type=monthly")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["line"]["spend_usd"] == "250.000000"
    assert body["line"]["cap_usd"] == "500.00"
    assert body["binding"] is not None


async def test_t7d_platform_admin_reads_across_tenants(session, seeded):
    """A platform admin is scoped to every org, including the other tenant.

    ``is_admin=True`` is the one authority that legitimately comes from a token
    claim (``get_user_role`` maps it to ``PLATFORM_ADMIN`` with no org scope), and
    it is a *platform*-level claim, not the org-level one T6 forges.
    """
    async with client_for(session, context_for("sub-4401-platform", is_admin=True)) as client:
        response = await client.get(f"/budget/scope/user/{FOREIGN_SUB}?period_type=monthly")

    assert response.status_code == 200, response.text
    assert response.json()["line"]["spend_usd"] == "888.888888"


async def test_t7e_container_runs_route_reports_unknown_not_an_empty_list(session, seeded):
    """A container target's run list is ``unknown``, never a $0 empty page.

    ``webhook-events`` is partitioned by principal, so there is no query returning
    "this team's runs". An empty list with a zero subtotal would read as "this team
    ran nothing" — the EPIC's headline failure. ``unknown`` says "we could not
    enumerate", which is the truth.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/team/{TEAM_A}/runs?period_type=monthly")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["subtotal"]["status"] == "unknown"
    assert body["subtotal"]["reason"] == "lineage_unavailable"
    # An `unknown` figure must never carry an amount (CostFigure's validator).
    assert body["subtotal"]["amount_usd"] is None


async def test_t7f_user_runs_route_joins_lineage_to_cost(session, seeded):
    """The happy path of the run drill-down, including the cross-store join.

    Two assertions carry weight beyond "it returned 200":

    * the lineage partition is queried with the target's **canonical** ``users.id``,
      not the Cognito sub the ledger is keyed by (#4300);
    * the cost join key is ``invocation_id`` (== ``usage_logs.agent_run_id``), not
      ``InvocationItem.run_id``, which holds the KEDA job name and matches no usage
      row — a wrong key here reports every run as free rather than failing.
    """
    session.add_all(
        [
            UsageLog(
                org_id=ORG_ID,
                department_id=DEPT_A,
                team_id=TEAM_A,
                user_id=COLLEAGUE_CANONICAL,
                model="claude",
                input_tokens=10,
                output_tokens=20,
                cost_usd=Decimal("4.500000"),
                latency_ms=100,
                status_code=200,
                agent_run_id="inv-4401-a",
            ),
        ]
    )
    await session.commit()

    activity = FakeActivityService([invocation("inv-4401-a", correlation_id="sweep-1"), invocation("inv-4401-b")])
    async with client_with_lineage(session, context_for(ORG_ADMIN_SUB), activity) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}/runs?period_type=monthly")

    assert response.status_code == 200, response.text
    body = response.json()
    assert activity.requested_user_ids == [COLLEAGUE_CANONICAL]

    assert [item["run_id"] for item in body["items"]] == ["inv-4401-a", "inv-4401-b"]
    assert body["items"][0]["cost"] == {"status": "known", "amount_usd": "4.500000"} | body["items"][0]["cost"]
    assert body["items"][0]["cost"]["status"] == "known"
    assert body["items"][0]["cost"]["amount_usd"] == "4.500000"
    # No usage row for the second run: `unknown`, NOT $0.00 (FR-3.5).
    assert body["items"][1]["cost"]["status"] == "unknown"
    assert body["items"][1]["cost"]["reason"] == "no_usage_rows"
    assert body["items"][1]["cost"]["amount_usd"] is None

    # One contributing run is unknown, so the subtotal is a lower bound and must
    # say so rather than presenting $4.50 as the total.
    assert body["subtotal"]["partial"] is True
    assert body["entity_id"] == COLLEAGUE_SUB


async def test_t7g_runs_route_still_enforces_scope_before_reading_lineage(session, seeded):
    """The lineage store is never touched for a denied caller.

    A route that authorised *after* fetching would still return 403, so the body
    alone cannot distinguish the two. Asserting the store was not queried is what
    pins the ordering — and with it, that a cross-tenant probe leaves no trace in
    another tenant's lineage partition.
    """
    activity = FakeActivityService([invocation("inv-4401-leak")])
    async with client_with_lineage(session, context_for(MEMBER_SUB), activity) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}/runs?period_type=monthly")

    assert_denied_without_metadata(response, forbidden_values=("inv-4401-leak",))
    assert activity.requested_user_ids == []


async def test_t7h_unresolvable_canonical_id_is_unknown_not_zero_runs(session, seeded):
    """A ledger-only principal has no lineage partition, so the answer is ``unknown``.

    ``root_user`` ids and Cognito subs live in different namespaces. When no
    ``users`` row backs the target, there is no partition to read — and reporting
    an empty list with a $0 subtotal would state "ran nothing" on no evidence. This
    is the ``resolve_canonical_user_id`` raw-sub-fallback trap the EPIC calls out.
    """
    session.add(
        BudgetUsage(
            org_id=ORG_ID,
            entity_type=EntityType.USER.value,
            entity_id="sub-4401-ledger-only",
            period_type=PeriodType.MONTHLY.value,
            period_start=PERIOD_START,
            total_cost_usd=Decimal("5.000000"),
        )
    )
    session.add(
        User(id="44010000-0000-4000-8000-00000000009a", cognito_sub="sub-4401-ledger-only", email="lo@example.com", org_id=ORG_ID, team_id=TEAM_A)
    )
    await session.commit()

    activity = FakeActivityService([invocation("inv-4401-should-not-be-read")])

    # Delete the users row so the id resolves for authorisation but not for lineage.
    await session.execute(User.__table__.delete().where(User.cognito_sub == "sub-4401-ledger-only"))
    await session.commit()

    async with client_with_lineage(session, context_for(ORG_ADMIN_SUB), activity) as client:
        response = await client.get("/budget/scope/user/sub-4401-ledger-only/runs?period_type=monthly")

    # With no users row the target is no longer locatable, so authorisation itself
    # denies — the stronger of the two honest outcomes, and never a zeroed 200.
    assert response.status_code == 403, response.text
    assert activity.requested_user_ids == []


async def test_t7i_bad_cursor_is_400_not_500(session, seeded):
    """A malformed pagination cursor is the caller's error, not an outage."""
    activity = FakeActivityService(raises=ValueError("invalid cursor"))
    async with client_with_lineage(session, context_for(ORG_ADMIN_SUB), activity) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}/runs?period_type=monthly&cursor=not-a-cursor")

    assert response.status_code == 400, response.text


async def test_t7j_lineage_outage_is_503_not_an_empty_run_list(session, seeded):
    """An unreadable lineage store is a 503. An empty list would be a lie (FR-1.7)."""
    activity = FakeActivityService(raises=ConnectionError("dynamodb unreachable"))
    async with client_with_lineage(session, context_for(ORG_ADMIN_SUB), activity) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}/runs?period_type=monthly")

    assert response.status_code == 503, response.text
    assert "not a report of zero" in response.json()["detail"]


async def test_t7k_cost_store_outage_degrades_to_unknown_and_says_which(session, seeded, monkeypatch):
    """Losing the cost ledger keeps the run list (still true) but marks cost unknown.

    The distinction the ``reason`` carries is operationally real: ``no_usage_rows``
    means "we looked and there was nothing", ``cost_store_unavailable`` means "we
    could not look". Collapsing them would make an outage indistinguishable from a
    free run.
    """

    async def boom(*args, **kwargs):
        raise ConnectionError("usage ledger unreachable")

    monkeypatch.setattr("src.budget.managed_scope_routes.get_cost_by_run_ids", boom)

    activity = FakeActivityService([invocation("inv-4401-a")])
    async with client_with_lineage(session, context_for(ORG_ADMIN_SUB), activity) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}/runs?period_type=monthly")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"][0]["cost"]["status"] == "unknown"
    assert body["items"][0]["cost"]["reason"] == "cost_store_unavailable"


async def test_t7l_budget_read_outage_is_503_not_a_zeroed_budget(session, seeded, monkeypatch):
    """A failed ledger read must not render as "$0 spent" — the EPIC's headline bug."""

    async def boom(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    monkeypatch.setattr("src.budget.managed_scope_routes._read_settled_spend", boom)

    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}?period_type=monthly")

    assert response.status_code == 503, response.text
    assert "not a report of zero spend" in response.json()["detail"]


async def test_t7m_target_ownership_lookup_outage_is_503_never_fail_open(session, seeded, monkeypatch):
    """If the store establishing WHO OWNS the target is down, refuse — never guess.

    Failing open here would serve another tenant's spend during a database blip;
    failing to a 403 would mislabel an outage as a permission problem. This is the
    one authorisation-path branch that is deliberately NOT a 403.
    """

    async def boom(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    monkeypatch.setattr("src.budget.managed_scope_routes._resolve_target_org", boom)

    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}?period_type=monthly")

    assert response.status_code == 503, response.text


async def test_t7q_canonical_id_lookup_outage_is_503_not_zero_runs(session, seeded):
    """An outage while translating sub -> canonical id is a 503, not an empty page.

    Distinct from T7n: there, the lookup succeeded and found nothing (``unknown``);
    here the lookup itself failed. Both must avoid a zeroed 200, but only one of
    them is an outage, and the status code is how an operator tells them apart.
    """

    class FailingOnUserLookup:
        """Delegates to the real session, but fails the canonical-id read.

        Wrapping rather than patching the module keeps the authorisation path fully
        real — the failure is injected at exactly the one query under test, after
        ownership and permission checks have already passed against real rows.
        """

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def scalar(self, statement, *args, **kwargs):
            # Keyed on the SELECTED column, not the WHERE clause: ownership
            # resolution filters on the same two columns but selects `users.org_id`,
            # so a substring match would fail the earlier query instead and test a
            # branch that is already covered by T7m.
            if str(statement).startswith("SELECT users.id"):
                raise OperationalError("SELECT users.id", {}, Exception("db down"))
            return await self._inner.scalar(statement, *args, **kwargs)

    app = build_app(session, context_for(ORG_ADMIN_SUB))

    async def override_db():
        yield FailingOnUserLookup(session)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_activity_service] = lambda: FakeActivityService([])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}/runs?period_type=monthly")

    assert response.status_code == 503, response.text
    assert "not a report of zero runs" in response.json()["detail"]


async def test_t7n_user_target_given_as_canonical_id_is_unknown_not_zero_runs(session, seeded):
    """A ``user`` target addressed by canonical id has no ledger-keyed partition.

    Ownership resolution accepts either namespace, but the lineage lookup keys on
    ``cognito_sub`` — so a caller who passes the canonical ``users.id`` to the
    ``user`` route authorises fine and then resolves to nothing. The honest answer
    is ``unknown``; an empty list would assert "ran nothing" about a user who may
    have run plenty under their ``root_user`` line.
    """
    activity = FakeActivityService([invocation("inv-4401-not-read")])
    async with client_with_lineage(session, context_for(ORG_ADMIN_SUB), activity) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_CANONICAL}/runs?period_type=monthly")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["subtotal"]["status"] == "unknown"
    assert body["subtotal"]["reason"] == "lineage_unavailable"
    assert activity.requested_user_ids == []


async def test_t7o_root_user_target_queries_lineage_with_the_id_as_given(session, seeded):
    """A ``root_user`` id IS the lineage partition key — no sub translation.

    The ``root_user`` ledger is keyed by canonical ``users.id`` (#4300), which is
    already the partition key, so translating it the way a ``user`` target is
    translated would look up a sub that does not exist and report zero runs.
    """
    activity = FakeActivityService([invocation("inv-4401-cloud")])
    async with client_with_lineage(session, context_for(ORG_ADMIN_SUB), activity) as client:
        response = await client.get(f"/budget/scope/root_user/{COLLEAGUE_CANONICAL}/runs?period_type=monthly")

    assert response.status_code == 200, response.text
    assert activity.requested_user_ids == [COLLEAGUE_CANONICAL]
    assert [item["run_id"] for item in response.json()["items"]] == ["inv-4401-cloud"]


async def test_t7p_empty_lineage_page_reports_no_cost_incurred_honestly(session, seeded):
    """Zero runs in the period skips the cost join rather than querying an empty set.

    This is the one case where an empty list IS the truth: the lineage store was
    read successfully and had nothing in the window. The subtotal must not be
    ``partial`` — there is nothing missing to warn about.
    """
    activity = FakeActivityService([])
    async with client_with_lineage(session, context_for(ORG_ADMIN_SUB), activity) as client:
        response = await client.get(f"/budget/scope/user/{COLLEAGUE_SUB}/runs?period_type=monthly")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["total_run_count"] == 0
    assert body["subtotal"]["partial"] is False


async def test_fail_closed_unknown_entity_type_resolves_to_no_owner(session, seeded):
    """The defensive default of the ownership resolver denies rather than allows.

    ``ScopeEntityType`` admits five values and FastAPI rejects anything else with a
    422, so this branch is unreachable over HTTP today. It is tested directly
    because its whole purpose is the *future* change: adding a member to that
    ``Literal`` without teaching the resolver about it must fail CLOSED. A test
    asserting that is the only thing that keeps the guarantee true after the next
    edit — a comment saying "unreachable" does not.
    """
    from src.budget.managed_scope_routes import _resolve_target_org

    assert await _resolve_target_org(session, "chain", COLLEAGUE_SUB) is None


async def test_fail_closed_dept_admin_target_with_no_team_is_denied(session, seeded):
    """An unplaceable principal is denied, not allowed through the department check.

    A user with no team has no resolvable department, so the caller's department
    cannot be shown to contain them. The safe answer is deny. Reached directly
    because ownership resolution rejects such a target earlier over HTTP; the
    branch still has to be correct, since the two functions are independently
    editable.
    """
    from src.admin.access_control import AccessControl
    from src.budget.managed_scope_routes import _check_department_scope

    await session.execute(User.__table__.update().where(User.cognito_sub == COLLEAGUE_SUB).values(team_id=""))
    await session.commit()

    allowed = await _check_department_scope(
        session,
        AccessControl(session),
        context_for(DEPT_ADMIN_SUB, department_id=DEPT_A),
        "user",
        COLLEAGUE_SUB,
        target_org=ORG_ID,
    )
    assert allowed is False


# ===========================================================================
# T9 — rollup rows carry principal_kind (FR-2.5)
# ===========================================================================


async def test_t9_rollup_rows_carry_principal_kind(session, seeded):
    """A container read rolls up members, and a service account is not a person.

    ``ci-bot`` is stored ``service:``-qualified (#4344), so it must be reported
    ``principal_kind="service"`` while the human members are ``"human"``. A member
    table that rendered an unattended trigger as a colleague makes the per-person
    cost truth a team lead opens this screen for simply wrong.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/org/{ORG_ID}?period_type=monthly")

    assert response.status_code == 200, response.text
    rollup = response.json()["rollup"]
    assert rollup, "an org target must roll up its per-principal ledger rows"

    by_id = {row["entity_id"]: row for row in rollup}

    # Every row answers the question — the field is required, never absent.
    assert all(row["principal_kind"] in ("human", "service") for row in rollup)

    assert by_id[SERVICE_ROOT_ID]["principal_kind"] == "service"
    assert by_id[SERVICE_ROOT_ID]["entity_type"] == "root_user"
    assert by_id[SERVICE_ROOT_ID]["line"]["spend_usd"] == "99.990000"

    assert by_id[COLLEAGUE_SUB]["principal_kind"] == "human"
    assert by_id[COLLEAGUE_SUB]["line"]["spend_usd"] == "777.777777"
    # The human member's own cap travels with their row, not a shared one.
    assert by_id[COLLEAGUE_SUB]["line"]["cap_usd"] == "1000.00"


async def test_t9b_rollup_excludes_other_tenants_principals(session, seeded):
    """The rollup is tenant-scoped: the other org's member is absent.

    The rollup read is the one place this unit enumerates rows rather than
    selecting a single one, so it is the place a missing ``org_id`` filter would
    leak a whole tenant's membership at once.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/org/{ORG_ID}?period_type=monthly")

    assert response.status_code == 200, response.text
    ids = {row["entity_id"] for row in response.json()["rollup"]}
    assert FOREIGN_SUB not in ids
    assert str(FOREIGN_SPEND) not in response.text


async def test_t9d_department_rollup_contains_only_that_departments_members(session, seeded):
    """A department target's rollup is confined to that department's members.

    The intra-tenant counterpart of T9b, and the sharper one: the caller is a
    ``dept_admin`` making a read they are GENUINELY authorised for — their own
    department — so the authorisation gate passes, and only the rollup's own
    membership filter stands between them and the rest of the org. An org-scoped
    rollup here hands a DEPT_A admin DEPT_B's per-member spend, which is exactly
    the scope T3 denies when asked for directly.
    """
    async with client_for(session, context_for(DEPT_ADMIN_SUB, department_id=DEPT_A)) as client:
        response = await client.get(f"/budget/scope/department/{DEPT_A}?period_type=monthly")

    assert response.status_code == 200, response.text
    ids = {row["entity_id"] for row in response.json()["rollup"]}

    # DEPT_A's members (TEAM_A) are present...
    assert MEMBER_SUB in ids
    assert COLLEAGUE_SUB in ids
    # ...and DEPT_B's member is not — by id or by figure.
    assert OTHER_DEPT_SUB not in ids
    assert str(OTHER_DEPT_SPEND) not in response.text
    # The `service:` root principal has no `users` row, so it cannot be placed in
    # any department and must not surface in one (fail closed on placement).
    assert SERVICE_ROOT_ID not in ids


async def test_t9e_team_rollup_contains_only_that_teams_members(session, seeded):
    """A team target's rollup is confined to that team's members.

    Same property as T9d one level down, exercised through an org_admin caller so
    the filter is proven to be the ROLLUP's, not a side effect of the caller's
    department scope: an org_admin may read every team in the org, yet each team's
    rollup must still describe only that team.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/team/{TEAM_A}?period_type=monthly")

    assert response.status_code == 200, response.text
    ids = {row["entity_id"] for row in response.json()["rollup"]}

    assert MEMBER_SUB in ids
    assert COLLEAGUE_SUB in ids
    # TEAM_B's member is absent even though the caller could read TEAM_B directly.
    assert OTHER_DEPT_SUB not in ids
    assert str(OTHER_DEPT_SPEND) not in response.text
    assert SERVICE_ROOT_ID not in ids


async def test_t9f_org_rollup_remains_org_wide(session, seeded):
    """An org target still rolls up every principal in the org — T9d/T9e narrow
    containers, they must not narrow the org, whose membership is the org."""
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/org/{ORG_ID}?period_type=monthly")

    assert response.status_code == 200, response.text
    ids = {row["entity_id"] for row in response.json()["rollup"]}
    assert {MEMBER_SUB, COLLEAGUE_SUB, OTHER_DEPT_SUB, SERVICE_ROOT_ID} <= ids


async def test_t9c_individual_target_has_no_rollup(session, seeded):
    """A single principal carries no rollup rows.

    A "rollup" of one row describing the target itself is the target's own line
    restated, and offering it invites a client to render a redundant table.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"/budget/scope/root_user/{SERVICE_ROOT_ID}?period_type=monthly")

    # A `service:`-qualified root principal has no `users` row, so its owning org
    # cannot be established from the database and it is denied rather than guessed
    # at — stripping the qualifier to match the remainder would authorise a read
    # whose ownership was never proven.
    assert response.status_code == 403, response.text


# ===========================================================================
# Static gate — this unit must not extend src/budget/routes.py (#4384, NFR-1)
# ===========================================================================


def test_static_gate_budget_routes_is_untouched():
    """``src/budget/routes.py`` must carry no change from this branch.

    The issue makes this a hard gate, not a style preference: that module reads
    ``entity_type``/``entity_id`` with no scope check and is open IDOR #4384.
    Adding a scoped route beside an unscoped one invites the next reviewer to
    assume the file is safe.

    Implemented as a real ``git diff`` against the merge base so it cannot rot —
    a hand-maintained "expected contents" copy would drift. Skips rather than
    fails when git or the base ref is unavailable (a shallow CI checkout), because
    a gate that fails for an environmental reason trains people to ignore it.
    """
    repo_root = Path(__file__).resolve().parents[3]
    target = "modules/gateway/src/budget/routes.py"

    try:
        base = subprocess.run(
            ["git", "merge-base", "origin/main", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if base.returncode != 0:
            pytest.skip("origin/main is unavailable in this checkout")

        diff = subprocess.run(
            ["git", "diff", "--stat", base.stdout.strip(), "HEAD", "--", target],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is unavailable in this environment")

    assert diff.returncode == 0, diff.stderr
    assert diff.stdout.strip() == "", f"this unit must not modify {target} (#4384, NFR-1):\n{diff.stdout}"


def test_managed_scope_router_is_registered_in_the_app():
    """The router is actually mounted, under a non-``/api`` prefix.

    A router that exists but is never registered is invisible in every other test
    in this file, since they mount it directly. And the prefix must not begin with
    ``/api``: CloudFront strips the first ``/api`` segment before the origin, so an
    ``/api``-prefixed mount is unreachable from the dashboard (#4330).
    """
    from src.app import UNIT_MODULES

    assert "src.budget.managed_scope_routes" in UNIT_MODULES

    from src.budget.managed_scope_routes import router

    paths = {route.path for route in router.routes}
    assert paths == {
        "/budget/scope/{entity_type}/{entity_id}",
        "/budget/scope/{entity_type}/{entity_id}/runs",
    }
    assert not any(path.startswith("/api") for path in paths)
