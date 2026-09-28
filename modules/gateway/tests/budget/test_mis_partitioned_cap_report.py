"""Mis-partitioned person-cap report — Issue #4627 (C2 of #4620).

The unit under test is a **detection** report: which ``root_user`` caps sit in a
partition where spend can never accrue (design note
``docs/design-notes/4620-cross-org-person-budgets.md`` §8.2). Two properties are
the acceptance gate, and they pull in opposite directions:

* **It must find the operator's cap.** The scenario fixture below is theirs,
  mechanically: a $5,000 monthly ``root_user`` cap in one partition, the matching
  settled accrual in another, the *same* canonical id in both (§2). A report that
  misses it is the whole issue unfixed.
* **It must not find a working cap, and must not leak while looking.** A
  correctly-partitioned cap is absent. And the cross-partition check crosses a
  tenant boundary to answer one boolean — §7.2 says foreign org ids and foreign
  dollar figures must not follow it back, so the foreign tenant here is seeded
  with figures and an id that would be unmistakable in a response body.

Harness: real in-memory SQLite with real ``User`` / ``UserIdentity`` /
``TenantMembership`` / ``BudgetConfig`` / ``BudgetUsage`` rows, and a real
``AccessControl`` resolving authority from real membership rows — the harness
``test_managed_scope_budget.py`` established. ``AccessControl`` is deliberately not
mocked: the authorisation is half of what this unit is, and a mocked
``check_permission`` asserts a guarantee it never exercised (the #4046 trap).
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.admin.config import AdminConfig, set_admin_config
from src.auth.dependencies import get_current_user
from src.budget.report_routes import router as report_router
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

# ---------------------------------------------------------------------------
# The operator's scenario, mechanically (note §2). HOME_ORG is where the cap was
# authored; RUN_ORG is where their agents actually execute and their spend
# accrues. The canonical id is the SAME string in both partitions — that is what
# makes the cap detectable, and what the report has to notice.
# ---------------------------------------------------------------------------

HOME_ORG = "org-4627-home"
RUN_ORG = "org-4627-run"

HOME_DEPT = "dept-4627-home"
RUN_DEPT = "dept-4627-run"

HOME_TEAM = "team-4627-home"
RUN_TEAM = "team-4627-run"

# The operator. Their cap is in HOME_ORG; their spend lands in RUN_ORG.
OPERATOR_SUB = "sub-4627-operator"
OPERATOR_CANONICAL = "46270000-0000-4000-8000-000000000001"

# A colleague whose cap is authored in the SAME partition their spend accrues in.
# The negative case: a correctly-partitioned cap must never be reported.
CORRECT_SUB = "sub-4627-correct"
CORRECT_CANONICAL = "46270000-0000-4000-8000-000000000002"

# A person who has never run anywhere. Their cap is dormant, which §8.2 says is
# NOT proof of misconfiguration — reported, but with `accrues_elsewhere=false`.
DORMANT_SUB = "sub-4627-dormant"
DORMANT_CANONICAL = "46270000-0000-4000-8000-000000000003"

# The §3.3 case: one GitHub account, TWO `users` rows (one per tenant), so two
# distinct `root_user` ledger keys. The cap is keyed on the home-org row; the
# accrual is keyed on the run-org row. `entity_id` equality alone misses this.
MULTIROW_SUB = "sub-4627-multirow"
MULTIROW_HOME_CANONICAL = "46270000-0000-4000-8000-000000000004"
MULTIROW_RUN_CANONICAL = "46270000-0000-4000-8000-000000000005"
MULTIROW_GITHUB_ID = "90014627"

# An unattended trigger with a cap (#4344). Its cap can be mis-partitioned like
# anyone's, and it must be labelled `service`, not rendered as a colleague.
SERVICE_ROOT_ID = "service:ci-bot-4627"

# Admin callers.
ORG_ADMIN_SUB = "sub-4627-orgadmin"
ORG_ADMIN_CANONICAL = "46270000-0000-4000-8000-000000000006"
DEPT_ADMIN_SUB = "sub-4627-deptadmin"
DEPT_ADMIN_CANONICAL = "46270000-0000-4000-8000-000000000007"
MEMBER_SUB = "sub-4627-member"
MEMBER_CANONICAL = "46270000-0000-4000-8000-000000000008"

# The operator's real cap, and figures chosen so a leak is unmistakable: the
# foreign partition's dollars are a distinctive string that must appear nowhere.
OPERATOR_CAP = Decimal("5000.00")
FOREIGN_SPEND = Decimal("4321.123456")
CORRECT_SPEND = Decimal("77.777777")
SERVICE_FOREIGN_SPEND = Decimal("1234.567890")
MULTIROW_FOREIGN_SPEND = Decimal("2468.135790")

PERIOD_START = date.today().replace(day=1)


@pytest.fixture(autouse=True)
def real_admin_config():
    """A REAL ``AdminConfig`` at its shipped defaults.

    Least-privilege default stays ``True`` — production behaviour, and what makes a
    no-membership principal resolve to ``MEMBER``. Overriding it would pin the
    rolled-back permissive fallback instead of the shipped one.
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
    """Two partitions, and every cap shape the report has to classify."""
    session.add_all(
        [
            Organization(id=HOME_ORG, name="Home Org 4627"),
            Organization(id=RUN_ORG, name="Run Org 4627"),
            Department(id=HOME_DEPT, org_id=HOME_ORG, name="Home Dept"),
            Department(id=RUN_DEPT, org_id=RUN_ORG, name="Run Dept"),
            Team(id=HOME_TEAM, org_id=HOME_ORG, department_id=HOME_DEPT, name="Home Team"),
            Team(id=RUN_TEAM, org_id=RUN_ORG, department_id=RUN_DEPT, name="Run Team"),
        ]
    )

    users = [
        (OPERATOR_CANONICAL, OPERATOR_SUB, HOME_ORG, HOME_TEAM, "Pat Operator", None),
        (CORRECT_CANONICAL, CORRECT_SUB, HOME_ORG, HOME_TEAM, "Correctly Partitioned", None),
        (DORMANT_CANONICAL, DORMANT_SUB, HOME_ORG, HOME_TEAM, "Never Ran", None),
        (MULTIROW_HOME_CANONICAL, MULTIROW_SUB, HOME_ORG, HOME_TEAM, "Two Rows Home", MULTIROW_GITHUB_ID),
        # The SAME person's second `users` row, in the other tenant (§3.3). Note
        # the deliberately different `cognito_sub`: independently onboarded.
        (MULTIROW_RUN_CANONICAL, f"{MULTIROW_SUB}-run", RUN_ORG, RUN_TEAM, "Two Rows Run", MULTIROW_GITHUB_ID),
        (ORG_ADMIN_CANONICAL, ORG_ADMIN_SUB, HOME_ORG, HOME_TEAM, "Org Admin", None),
        (DEPT_ADMIN_CANONICAL, DEPT_ADMIN_SUB, HOME_ORG, HOME_TEAM, "Dept Admin", None),
        (MEMBER_CANONICAL, MEMBER_SUB, HOME_ORG, HOME_TEAM, "Plain Member", None),
    ]
    roles = {
        ORG_ADMIN_CANONICAL: "org_admin",
        DEPT_ADMIN_CANONICAL: "dept_admin",
    }
    for canonical, sub, org, team, name, github_id in users:
        session.add(User(id=canonical, cognito_sub=sub, email=f"{sub}@example.com", name=name, org_id=org, team_id=team))
        # Authority lives HERE — tenant_memberships, never the token.
        session.add(TenantMembership(user_id=canonical, tenant_id=org, role=roles.get(canonical, "member"), is_active=True))
        if github_id:
            session.add(
                UserIdentity(
                    user_id=canonical,
                    org_id=org,
                    team_id=team,
                    provider="github",
                    provider_user_id=github_id,
                    verification_method="oauth",
                )
            )

    session.add_all(
        [
            # ---- The operator's cap: authored in HOME_ORG, never accrues here. ----
            BudgetConfig(
                org_id=HOME_ORG,
                entity_type=EntityType.ROOT_USER.value,
                entity_id=OPERATOR_CANONICAL,
                period_type=PeriodType.MONTHLY.value,
                budget_amount_usd=OPERATOR_CAP,
                enforcement_mode="hard",
            ),
            # ...and the real spend, in the OTHER partition, same canonical id.
            BudgetUsage(
                org_id=RUN_ORG,
                entity_type=EntityType.ROOT_USER.value,
                entity_id=OPERATOR_CANONICAL,
                period_type=PeriodType.MONTHLY.value,
                period_start=PERIOD_START,
                total_cost_usd=FOREIGN_SPEND,
            ),
            # ---- A correctly-partitioned cap: cap and accrual in HOME_ORG. ----
            BudgetConfig(
                org_id=HOME_ORG,
                entity_type=EntityType.ROOT_USER.value,
                entity_id=CORRECT_CANONICAL,
                period_type=PeriodType.MONTHLY.value,
                budget_amount_usd=Decimal("300.00"),
                enforcement_mode="soft",
            ),
            BudgetUsage(
                org_id=HOME_ORG,
                entity_type=EntityType.ROOT_USER.value,
                entity_id=CORRECT_CANONICAL,
                period_type=PeriodType.MONTHLY.value,
                period_start=PERIOD_START,
                total_cost_usd=CORRECT_SPEND,
            ),
            # ---- A dormant cap: no accrual anywhere, in any partition. ----
            BudgetConfig(
                org_id=HOME_ORG,
                entity_type=EntityType.ROOT_USER.value,
                entity_id=DORMANT_CANONICAL,
                period_type=PeriodType.MONTHLY.value,
                budget_amount_usd=Decimal("42.00"),
                enforcement_mode="soft",
            ),
            # ---- §3.3: cap on the home row, accrual on the run row. ----
            BudgetConfig(
                org_id=HOME_ORG,
                entity_type=EntityType.ROOT_USER.value,
                entity_id=MULTIROW_HOME_CANONICAL,
                period_type=PeriodType.MONTHLY.value,
                budget_amount_usd=Decimal("900.00"),
                enforcement_mode="hard",
            ),
            BudgetUsage(
                org_id=RUN_ORG,
                entity_type=EntityType.ROOT_USER.value,
                entity_id=MULTIROW_RUN_CANONICAL,
                period_type=PeriodType.MONTHLY.value,
                period_start=PERIOD_START,
                total_cost_usd=MULTIROW_FOREIGN_SPEND,
            ),
            # ---- A service-rooted cap, mis-partitioned the same way (#4344). ----
            BudgetConfig(
                org_id=HOME_ORG,
                entity_type=EntityType.ROOT_USER.value,
                entity_id=SERVICE_ROOT_ID,
                period_type=PeriodType.DAILY.value,
                budget_amount_usd=Decimal("15.00"),
                enforcement_mode="hard",
            ),
            BudgetUsage(
                org_id=RUN_ORG,
                entity_type=EntityType.ROOT_USER.value,
                entity_id=SERVICE_ROOT_ID,
                period_type=PeriodType.DAILY.value,
                period_start=PERIOD_START,
                total_cost_usd=SERVICE_FOREIGN_SPEND,
            ),
            # ---- Noise the predicate must ignore: a `user` cap with no accrual
            # (different ledger, different id namespace) and an `org` cap. Neither
            # is a person-cap partitioning question.
            BudgetConfig(
                org_id=HOME_ORG,
                entity_type=EntityType.USER.value,
                entity_id=OPERATOR_SUB,
                period_type=PeriodType.MONTHLY.value,
                budget_amount_usd=Decimal("111.00"),
                enforcement_mode="hard",
            ),
            BudgetConfig(
                org_id=HOME_ORG,
                entity_type=EntityType.ORGANIZATION.value,
                entity_id=HOME_ORG,
                period_type=PeriodType.MONTHLY.value,
                budget_amount_usd=Decimal("99999.00"),
                enforcement_mode="hard",
            ),
            # ---- Another tenant's dormant `root_user` cap. The report is scoped
            # to the caller's partition, so this must never appear in it.
            BudgetConfig(
                org_id=RUN_ORG,
                entity_type=EntityType.ROOT_USER.value,
                entity_id="46270000-0000-4000-8000-00000000ffff",
                period_type=PeriodType.MONTHLY.value,
                budget_amount_usd=Decimal("6543.00"),
                enforcement_mode="hard",
            ),
        ]
    )
    await session.commit()


def context_for(sub: str, *, org_id: str = HOME_ORG, team_id: str = HOME_TEAM, department_id: str = HOME_DEPT, **overrides) -> TokenContext:
    """A token context. Note what it does NOT establish: authority.

    ``is_admin`` is only set by a *platform*-level claim, so a caller's role comes
    from their ``tenant_memberships`` row rather than from this object — which is
    what the forged-claim test below exploits.
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
    """Mount the report router alone, with auth and db overridden.

    ``AccessControl`` is deliberately NOT overridden — it is constructed inside the
    route against the request's real session, so the authority checks run against
    the real ``tenant_memberships`` rows.
    """
    app = FastAPI()
    app.include_router(report_router)

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: context
    return app


def client_for(session: AsyncSession, context: TokenContext) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=build_app(session, context)), base_url="http://test")


REPORT_PATH = "/budget/reports/mis-partitioned-caps"


def rows_by_entity(body: dict) -> dict[str, dict]:
    return {row["entity_id"]: row for row in body["rows"]}


def assert_denied_without_metadata(response, *, forbidden_values: tuple[str, ...] = ()) -> None:
    """Assert the uniform ``403`` that reveals nothing about the partition.

    Asserted for equality against the single canonical message rather than a
    substring: a denial whose body varies by caller is an enumeration oracle even
    when it is consistently a 403.
    """
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "Not authorized to read budget data for the requested scope."}, response.text
    for value in forbidden_values:
        assert value not in response.text, f"denial leaked {value!r}: {response.text}"


# ===========================================================================
# The acceptance gate: the operator's cap is found, a working cap is not
# ===========================================================================


async def test_operator_mis_partitioned_cap_is_reported(session, seeded):
    """The issue's headline: the operator's dormant $5,000 cap is in the report.

    Cap in ``HOME_ORG``, accrual in ``RUN_ORG``, same canonical id (§2). It is
    reported, and ``accrues_elsewhere`` is ``true`` — the confirmed
    mis-partitioned case, as distinct from a cap whose owner has simply not run.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    row = rows_by_entity(body)[OPERATOR_CANONICAL]

    assert row["accrues_elsewhere"] is True
    assert row["cap_usd"] == "5000.00"
    assert row["org_id"] == HOME_ORG
    assert row["period_type"] == PeriodType.MONTHLY.value
    assert row["enforcement_mode"] == "hard"
    assert row["principal_kind"] == "human"
    assert row["display_name"] == "Pat Operator"


async def test_correctly_partitioned_cap_is_not_reported(session, seeded):
    """A cap whose accrual lands in its own partition is absent.

    The other half of the gate. §8.2's predicate is ``NOT EXISTS`` a matching
    accrual, so a working cap is excluded before the cross-partition question is
    ever asked — which is why this holds regardless of ``accrues_elsewhere``.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    assert response.status_code == 200, response.text
    assert CORRECT_CANONICAL not in rows_by_entity(response.json())


async def test_dormant_cap_is_reported_but_not_confirmed(session, seeded):
    """A cap with no accrual anywhere is reported with ``accrues_elsewhere=false``.

    §8.2 verbatim: "a dormant cap is not proof of misconfiguration (a person may
    simply not have run yet), which is exactly why this reports rather than
    migrates." So it appears — an operator auditing caps wants to see it — but it
    is not counted as a confirmed defect.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    body = response.json()
    assert body["rows"], body
    assert rows_by_entity(body)[DORMANT_CANONICAL]["accrues_elsewhere"] is False
    assert body["accrues_elsewhere_count"] == sum(1 for row in body["rows"] if row["accrues_elsewhere"])
    assert body["total_row_count"] == len(body["rows"])


async def test_confirmed_rows_sort_before_dormant_ones(session, seeded):
    """Confirmed defects come first, so an operator sees them without scrolling."""
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    flags = [row["accrues_elsewhere"] for row in response.json()["rows"]]
    assert flags == sorted(flags, reverse=True), flags


# ===========================================================================
# §3.3 — the multi-`users`-row person, the population this report is for
# ===========================================================================


async def test_multi_users_row_person_is_detected_via_github_anchor(session, seeded):
    """One GitHub account, two ``users`` rows, two ledger keys — still detected.

    The §3.3 / §11-caveat-3 case, and the one a naive implementation gets wrong:
    the cap is keyed on the home-org ``users.id`` while the accrual is keyed on the
    run-org one, so comparing ``entity_id`` alone finds no match and reports the
    cap as merely dormant. Resolving the person through
    ``user_identities.provider_user_id`` is what makes ``accrues_elsewhere`` true
    here.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    row = rows_by_entity(response.json())[MULTIROW_HOME_CANONICAL]
    assert row["accrues_elsewhere"] is True, "the person's second users.id was not followed — §3.3 under-report"
    assert row["cap_usd"] == "900.00"


async def test_unlinked_person_still_gets_the_same_id_comparison(session, seeded):
    """No GitHub identity linked is not a reason to miss the common case.

    The operator themselves has no ``user_identities`` row in this fixture, so the
    anchor expansion contributes nothing for them — and their cap is still
    confirmed, because the single-``users``-row comparison on the cap's own id is
    always included. A person set that replaced rather than extended the plain id
    would regress the headline scenario.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    identities = (await session.execute(UserIdentity.__table__.select())).all()
    assert all(row.user_id != OPERATOR_CANONICAL for row in identities), "fixture drift: the operator must have no linked identity"
    assert rows_by_entity(response.json())[OPERATOR_CANONICAL]["accrues_elsewhere"] is True


# ===========================================================================
# The predicate: what is and is not a person-cap partitioning question
# ===========================================================================


async def test_only_root_user_caps_are_reported(session, seeded):
    """``user``/``org`` caps are not in scope, even with no matching accrual.

    §8.2's predicate names ``entity_type='root_user'`` and only that. A ``user``
    cap is keyed by Cognito sub in a different id namespace, and an ``org`` cap is
    not a person cap at all — reporting either would make the operator chase a cap
    that is working as designed.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    body = response.json()
    assert OPERATOR_SUB not in rows_by_entity(body), "a `user` cap leaked into a root_user report"
    assert HOME_ORG not in rows_by_entity(body), "an `org` cap leaked into a root_user report"
    assert "111.00" not in response.text
    assert "99999.00" not in response.text


async def test_report_is_scoped_to_the_callers_partition(session, seeded):
    """Another tenant's dormant ``root_user`` cap never appears.

    The partition is resolved from the caller's ``tenant_memberships`` row, so the
    foreign cap is out of the query entirely. Asserted on its distinctive amount
    as well as its id: an ``org_id`` filter dropped from the predicate would
    surface it and nothing else in the shape would look wrong.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    body = response.json()
    assert body["org_id"] == HOME_ORG
    assert all(row["org_id"] == HOME_ORG for row in body["rows"]), body["rows"]
    assert "6543.00" not in response.text


async def test_accrual_in_a_different_period_type_does_not_suppress_the_row(session, seeded):
    """Matching is on ``period_type`` too — a weekly accrual does not settle a monthly cap.

    §8.2's ``NOT EXISTS`` agrees on ``(org_id, entity_type, entity_id,
    period_type)``. Dropping ``period_type`` would let the tracker's *weekly* row —
    which it writes for every request alongside the daily and monthly ones — mark a
    monthly cap as accruing, hiding a genuinely mis-partitioned cap.
    """
    session.add(
        BudgetUsage(
            org_id=HOME_ORG,
            entity_type=EntityType.ROOT_USER.value,
            entity_id=OPERATOR_CANONICAL,
            period_type=PeriodType.WEEKLY.value,
            period_start=PERIOD_START,
            total_cost_usd=Decimal("1.500000"),
        )
    )
    await session.commit()

    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    assert OPERATOR_CANONICAL in rows_by_entity(response.json()), "a weekly accrual suppressed a monthly cap's row"


async def test_own_partition_accrual_in_an_older_period_suppresses_the_row(session, seeded):
    """ "Has never accrued here" is a lifetime property, not a this-month one.

    §8.2's SQL carries no ``period_start`` filter, deliberately: a cap that accrued
    in its own partition last month is correctly partitioned and merely quiet now.
    Adding the current period would report every such cap and drown the real signal
    in noise.
    """
    session.add(
        BudgetUsage(
            org_id=HOME_ORG,
            entity_type=EntityType.ROOT_USER.value,
            entity_id=OPERATOR_CANONICAL,
            period_type=PeriodType.MONTHLY.value,
            period_start=date(2020, 1, 1),
            total_cost_usd=Decimal("3.250000"),
        )
    )
    await session.commit()

    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    assert OPERATOR_CANONICAL not in rows_by_entity(response.json())


async def test_service_rooted_cap_is_reported_and_labelled_service(session, seeded):
    """An unattended trigger's cap is reported as dormant, never confirmed.

    Present because its cap can be mis-partitioned exactly like a human's, and
    ``principal_kind="service"`` (#4344) stops the operator reading ``ci-bot`` as
    a colleague. But ``accrues_elsewhere`` is FALSE even though this fixture's
    accrual really is in another partition (review fix): a service key is a
    resource string any admin can author a cap on, with no per-person identity
    anchor behind it — a cross-tenant existence bit on it would be a one-bit
    probe into another tenant's automation activity (§7.2). The operator still
    sees the dormant row and investigates; the platform just makes no
    cross-tenant claim on a key it cannot tie to a person.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    row = rows_by_entity(response.json())[SERVICE_ROOT_ID]
    assert row["principal_kind"] == "service"
    assert row["display_name"] is None
    assert row["accrues_elsewhere"] is False
    assert row["period_type"] == PeriodType.DAILY.value


# ===========================================================================
# §7.2 — what must NOT cross the tenant boundary
# ===========================================================================


async def test_no_foreign_org_id_or_figure_reaches_the_response(session, seeded):
    """The cross-partition check answers a boolean and leaks nothing else.

    §7.2: a dollar total is the other tenant's cost data, and disclosing it to an
    admin of an unrelated org because a person is shared has no membership basis.
    Every foreign figure in the fixture is distinctive, so any of them appearing
    here fails loudly rather than coincidentally.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    assert response.status_code == 200, response.text
    for leaked in (RUN_ORG, str(FOREIGN_SPEND), "4321", str(SERVICE_FOREIGN_SPEND), "1234", str(MULTIROW_FOREIGN_SPEND), "2468"):
        assert leaked not in response.text, f"foreign data leaked: {leaked!r}"


async def test_response_carries_no_foreign_person_identifiers(session, seeded):
    """Not even the foreign ``users.id`` of the shared person crosses.

    The anchor expansion reads the person's other ``users.id`` values in order to
    answer the boolean; those ids are an internal comparison key, not output. A row
    echoes the cap's OWN key, which is the string the operator has to match to fix
    the cap.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    assert MULTIROW_RUN_CANONICAL not in response.text
    assert MULTIROW_HOME_CANONICAL in response.text


# ===========================================================================
# Authorisation — #4384's caution: this surface must not widen
# ===========================================================================


async def test_plain_member_is_denied(session, seeded):
    """``MEMBER`` does not hold ``BUDGET_READ``, and learns nothing from the denial."""
    async with client_for(session, context_for(MEMBER_SUB)) as client:
        response = await client.get(REPORT_PATH)

    assert_denied_without_metadata(response, forbidden_values=("5000.00", OPERATOR_CANONICAL, RUN_ORG))


async def test_dept_admin_is_denied(session, seeded):
    """A ``dept_admin`` holds ``BUDGET_READ`` but is still denied.

    The report's scope is the whole partition — a ``budget_configs`` row carries no
    team or department edge, so a dept-narrowed view of it cannot be expressed.
    Denied rather than served a partially-filtered list, on the same rule that
    makes ``managed_scope_routes`` deny a ``dept_admin`` an ``org`` target: a filter
    that cannot be expressed must not be approximated.
    """
    async with client_for(session, context_for(DEPT_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    assert_denied_without_metadata(response, forbidden_values=("5000.00", OPERATOR_CANONICAL))


async def test_forged_role_claim_grants_nothing(session, seeded):
    """A token claiming ``org_admin`` does not make the caller one.

    Authority is resolved from ``tenant_memberships``; the token supplies identity
    only. Pinned in two halves (review fix — the original only asserted the
    denial, which a plain member gets anyway, so it could never fail): first that
    ``TokenContext`` DROPS the unknown ``role`` kwarg (pydantic extra-ignore), so
    the forged claim structurally cannot reach any authz path; then the denial.
    If TokenContext ever grows a ``role`` field, the hasattr assertion fails and
    this test must be rewritten to forge the claim end-to-end.
    """
    context = context_for(MEMBER_SUB, role="org_admin")
    assert not hasattr(context, "role"), "TokenContext grew a role field — forge the claim for real"

    async with client_for(session, context) as client:
        response = await client.get(REPORT_PATH)

    assert_denied_without_metadata(response)


async def test_caller_with_no_membership_is_denied(session, seeded):
    """An authenticated principal with no membership row gets nothing.

    Least-privilege default resolves them to ``MEMBER``, which is denied — the
    #60 rule, and the alternative (a 200 with an empty report) would be a silent
    RBAC bypass that reads as "your partition is clean".
    """
    async with client_for(session, context_for("sub-4627-nobody")) as client:
        response = await client.get(REPORT_PATH)

    assert_denied_without_metadata(response)


async def test_the_report_accepts_no_partition_parameter(session, seeded):
    """A caller cannot point the report at another tenant.

    There is no ``org_id`` parameter to forge, so a query string naming the foreign
    tenant is simply not read and the caller receives their own partition. This is
    the structural half of the authorisation — the reason this router has no IDOR
    to have.
    """
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(f"{REPORT_PATH}?org_id={RUN_ORG}&entity_id={MULTIROW_RUN_CANONICAL}")

    assert response.status_code == 200, response.text
    assert response.json()["org_id"] == HOME_ORG
    assert "6543.00" not in response.text


async def test_platform_admin_reads_their_active_session_tenant(session, seeded):
    """A platform admin resolves to no membership row, and reports on their token's org.

    ``get_user_role`` returns ``(PLATFORM_ADMIN, None, None)`` from the claim alone,
    so there is no membership-derived partition — the active session tenant is used,
    the same partition every other budget read serves them. Asserted because the
    fallback is the one branch where the partition does not come from a membership
    row, and defaulting it to "all partitions" would be an unscoped cross-tenant
    read.
    """
    context = context_for("sub-4627-platform", is_admin=True)
    async with client_for(session, context) as client:
        response = await client.get(REPORT_PATH)

    assert response.status_code == 200, response.text
    assert response.json()["org_id"] == HOME_ORG


# ===========================================================================
# A failed read is never an empty report
# ===========================================================================


async def test_ledger_failure_is_a_503_not_an_empty_report(session, seeded, monkeypatch):
    """An unreadable ledger raises ``503``; it must not render as "no findings".

    "No mis-partitioned caps" and "the check could not run" are opposite claims,
    and an operator who reads the second as the first concludes their caps are fine
    during an outage. The same rule ``/me/budget`` and the managed-scope routes
    already apply to a zeroed figure.
    """
    import src.budget.report_routes as report_routes

    async def boom(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("ledger down"))

    monkeypatch.setattr(report_routes, "_read_dormant_root_user_caps", boom)

    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    assert response.status_code == 503, response.text
    assert "not a report of zero findings" in response.json()["detail"]


async def test_authority_store_failure_is_a_503_not_a_denial(session, seeded, monkeypatch):
    """An unreadable authority store is ``503``, never a fallthrough to allow.

    A 403 would mislabel an outage as a permission problem; a 200 would serve a
    partition to a caller whose role was never resolved. Neither is acceptable, so
    the read is refused.
    """
    from src.admin.access_control import AccessControl

    async def boom(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("memberships down"))

    monkeypatch.setattr(AccessControl, "get_user_role", boom)

    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)

    assert response.status_code == 503, response.text


# ===========================================================================
# Contract pins
# ===========================================================================


def test_denial_message_matches_the_managed_scope_router():
    """The two denials must read identically.

    Duplicated as a constant for readability in one file, pinned here so they
    cannot drift: a report denial worded differently from a scope denial tells a
    prober which surface they hit.
    """
    from src.budget import managed_scope_routes, report_routes

    assert report_routes._DENIAL_DETAIL == managed_scope_routes._DENIAL_DETAIL


def test_router_declares_no_mutating_route():
    """Detection, not mutation (§8.1/§8.2) — asserted structurally, not by review.

    The note's ruling is that existing caps stay in place, so the absence of a
    write endpoint is a contract rather than an omission. A future POST/PATCH/PUT/
    DELETE on this router breaks CI here instead of silently shipping the automatic
    re-partitioning §8.1 rules out.
    """
    from src.budget.report_routes import router

    methods = {method for route in router.routes for method in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD", "OPTIONS"}, f"the report router declares mutating methods: {sorted(methods)}"


def test_router_is_mounted_and_not_shadowed_by_the_scope_router():
    """The report path is reachable on the real app, ahead of any wildcard.

    The reason this is a separate module: ``/budget/scope``'s
    ``/{entity_type}/{entity_id}`` route would shadow a literal sibling added after
    it, answering a valid report request with a 422. Asserted on the built app so
    the mount — not just the module — is what is checked.
    """
    from src.app import create_app

    paths = {getattr(route, "path", "") for route in create_app().routes}
    assert REPORT_PATH in paths, f"{REPORT_PATH} is not mounted; the dashboard's /api call would 404"
    assert not REPORT_PATH.startswith("/budget/scope/"), "the report must not live under the wildcard-bearing scope prefix"


async def test_org_admin_is_denied_while_the_rbac_rollback_lever_is_active(session, seeded, monkeypatch):
    """Under BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT=false, org admins get a uniform 403.

    Review fix: the rollback lever makes get_user_role's no-membership fallback
    grant ORG_ADMIN, and the route cannot tell a row-backed admin from the
    fallback — so this NEW org-wide enumeration fails closed for the lever's
    duration rather than serving every colleague's cap to an unestablished
    principal. Platform admins are unaffected (asserted below).
    """
    from src.budget import report_routes as module

    class _RolledBack:
        rbac_least_privilege_default = False

    monkeypatch.setattr(module, "get_admin_config", lambda: _RolledBack())

    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        response = await client.get(REPORT_PATH)
    assert_denied_without_metadata(response)

    async with client_for(session, context_for("sub-4627-platform", is_admin=True)) as client:
        response = await client.get(REPORT_PATH)
    assert response.status_code == 200
