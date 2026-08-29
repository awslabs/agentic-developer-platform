"""Own-scope budget read API — Issue #4397 (U-1 of EPIC #4324).

``GET /me/budget`` is the keystone of the EPIC: U-2…U-5 all consume or mirror its
contract, so its shape is load-bearing and every field rule below is a gate.

**Authz negatives come first** (NFR-3), before any happy path: an unauthenticated
request must be rejected, and a request naming somebody else must return the
*caller's own* figures. Those are the tests that would catch #4384's IDOR being
re-created in a new file, so they are written and run first.

Harness notes:

* Real in-memory SQLite with real ``BudgetConfig``/``BudgetUsage`` rows, the
  harness from ``test_service.py``/``test_noncalendar_config_rows.py``. Rows are
  inserted as raw models rather than through ``create_budget`` deliberately: the
  point is what happens when these values are already in the database, however
  they got there.
* Config overrides use a **real** ``BudgetConfig`` settings object with
  ``object.__setattr__``, never a ``MagicMock`` — a fully-patched config asserts a
  guarantee it never exercised (the #4046 trap).
* The parity test (T7) asserts against ``_check_entity_budget``'s **own** figure,
  never a hand-computed number and never a fixed over-report factor: the
  divergence #4328 caused is data-dependent, so a hard-coded expectation would
  pass while the predicate was wrong.
"""

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.auth.dependencies import get_current_user
from src.budget.config import BudgetConfig as BudgetSettings
from src.budget.config import budget_config
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.me_routes import router as me_budget_router
from src.budget.schemas import format_money
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

ORG_ID = "org-4397"

# The caller. Their DIRECT spend is keyed by Cognito sub under entity_type="user";
# the spend of chains they trigger is keyed by canonical users.id under
# "root_user" (#4300). Two namespaces, deliberately non-colliding — see T13/T14.
CALLER_SUB = "sub-caller-4397"
CALLER_CANONICAL_ID = "11111111-1111-4111-8111-111111111111"

# A colleague in the same org, used by the IDOR tests. Seeded with figures that
# are unmistakably not the caller's, so an endpoint leaking them fails loudly
# rather than coincidentally matching.
#
# The id deliberately sorts BEFORE `CALLER_SUB` ("sub-aaa…" < "sub-caller…"), and
# that is load-bearing rather than cosmetic. See the note below on decoy ordering.
OTHER_SUB = "sub-aaa-colleague-4397"

# ---------------------------------------------------------------------------
# Why decoy rows are ordered the way they are
# ---------------------------------------------------------------------------
# Every read in `me_routes.py` selects a SINGLE row on a predicate that matches
# the table's unique constraint. So dropping a filter from one of those
# predicates does not raise and does not return a wrong *sum* — it silently
# returns whichever still-matching row the database happens to hand back first.
# A decoy row only makes such a mutation observable if the decoy is the row that
# comes back first, and which row that is depends on WHICH filter was dropped:
#
#   * Drop a NON-leading column (`entity_id`, `period_start`) — `org_id` is still
#     in the predicate, so SQLite can still seek the `uq_budget_usage` /
#     `uq_budget_config` index and scans within that prefix in **index key**
#     order. Insertion order is irrelevant; the decoy must sort earlier by key,
#     which is why `OTHER_SUB` starts "sub-aaa".
#   * Drop the LEADING column (`org_id`) — the index is no longer seekable, so
#     the plan degrades to a table scan in rowid order, i.e. **insertion** order.
#     There the decoy must be INSERTED first.
#
# Both shapes are used below, and every filter-dropping mutation was verified to
# fail at least one test. Reordering these seeds, or renaming `OTHER_SUB` to
# something sorting after `CALLER_SUB`, silently re-opens that hole: the suite
# still passes while the read predicate leaks another member's ledger.


@pytest.fixture
async def engine():
    """In-memory SQLite with the real schema."""
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
async def caller_user_row(session) -> None:
    """The ``users`` row that lets the Cognito sub resolve to a canonical id.

    Without it ``resolve_canonical_user_id`` falls back to the raw sub, which is
    the ``identity_status="unresolved"`` path asserted by T14. Every test that
    wants a *resolved* identity depends on this fixture.
    """
    # `team_id` is NOT NULL on `users`; the caller's *budget* hierarchy still has
    # no team unless a test sets `team_id` on the token, which is a separate field.
    session.add(User(id=CALLER_CANONICAL_ID, cognito_sub=CALLER_SUB, email="caller@example.com", org_id=ORG_ID, team_id=""))
    await session.commit()


def caller_context(**overrides) -> TokenContext:
    """A human caller with no team or department unless a test adds one."""
    defaults = {
        "user_id": CALLER_SUB,
        "org_id": ORG_ID,
        "team_id": "",
        "department_id": "",
        "account_type": "human",
        "expires_at": date(2099, 1, 1),
    }
    return TokenContext(**{**defaults, **overrides})


def build_app(session: AsyncSession, context: TokenContext | None = None) -> FastAPI:
    """Mount the router alone with auth and db overridden.

    Mounting just this router (rather than the whole app) keeps these tests
    focused on the endpoint's own behaviour; that the router is *registered* in
    the real app is asserted separately by T17.
    """
    app = FastAPI()
    app.include_router(me_budget_router)

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    if context is not None:
        app.dependency_overrides[get_current_user] = lambda: context
    return app


def client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def seed_cap(
    session: AsyncSession,
    entity_type: EntityType,
    entity_id: str,
    period_type: PeriodType,
    amount: str,
    enforcement_mode: str = "hard",
    org_id: str = ORG_ID,
) -> None:
    session.add(
        BudgetConfig(
            org_id=org_id,
            entity_type=entity_type.value,
            entity_id=entity_id,
            period_type=period_type.value,
            budget_amount_usd=Decimal(amount),
            enforcement_mode=enforcement_mode,
        )
    )
    await session.commit()


async def seed_usage(
    session: AsyncSession,
    entity_type: EntityType,
    entity_id: str,
    period_type: PeriodType,
    amount: str,
    period_start: date | None = None,
    org_id: str = ORG_ID,
) -> None:
    from src.budget.utils import get_period_start_end

    start = period_start or get_period_start_end(period_type)[0]
    session.add(
        BudgetUsage(
            org_id=org_id,
            entity_type=entity_type.value,
            entity_id=entity_id,
            period_type=period_type.value,
            period_start=start,
            total_cost_usd=Decimal(amount),
            total_tokens=1000,
            request_count=1,
        )
    )
    await session.commit()


# ===========================================================================
# Authz negatives — FIRST, per NFR-3
# ===========================================================================


class TestAuthorizationNegatives:
    """The tests that must exist before any happy path.

    #4384 is an open IDOR on ``src/budget/routes.py``: it reads
    ``entity_type``/``entity_id`` straight off the request, so any member can read
    any colleague's cap and spend. This unit's whole placement constraint (NFR-1)
    exists to avoid inheriting that, and these are the tests that prove it did.
    """

    async def test_unauthenticated_request_is_rejected(self, session):
        """T1 — no token, no figures (FR-1.2).

        ``get_current_user`` is deliberately NOT overridden here, so the real
        dependency runs and rejects the request. Asserting "not 200" rather than a
        specific code: the point is that spend data does not leak without a token,
        and both 401 and 403 satisfy that.
        """
        app = build_app(session)  # no auth override
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert response.status_code in (401, 403), (
            f"unauthenticated request returned {response.status_code}; budget figures must not be readable without a token"
        )
        assert "spend_usd" not in response.text

    async def test_user_id_param_naming_another_user_is_ignored(self, session, caller_user_row):
        """T2 — ``?user_id=<colleague>`` returns the CALLER's figures (FR-1.2).

        The param is ignored, not honoured. Seeded so the two users' spends are
        unmistakably different: an endpoint that honoured the param would return
        the colleague's $999 and fail on the value, not merely on a code.

        This is the test that pins the ``entity_id`` filter — the one that keeps
        one member's ledger out of another's response. It only does so because
        ``OTHER_SUB`` sorts before ``CALLER_SUB``; see the decoy-ordering note at
        the top of this module for why the sort key, not the insert order, is what
        matters for a dropped non-leading filter. Verified by mutation.
        """
        await seed_cap(session, EntityType.USER, OTHER_SUB, PeriodType.MONTHLY, "5000.00")
        await seed_usage(session, EntityType.USER, OTHER_SUB, PeriodType.MONTHLY, "999.00")
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "100.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "10.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget", params={"user_id": OTHER_SUB})

        assert response.status_code == 200
        body = response.json()
        assert body["spend_usd"] == format_money(Decimal("10.00"), Decimal("0.000001"))
        assert body["cap_usd"] == "100.00"
        assert "999" not in response.text, "the colleague's spend appears in the response — the user_id param was honoured"

    async def test_entity_id_and_entity_type_params_are_ignored(self, session, caller_user_row):
        """T3 — the #4384 parameter pair specifically, on the new surface.

        ``entity_type``/``entity_id`` are the exact params that make
        ``src/budget/routes.py`` an IDOR. Naming an org-level ledger with them
        must not switch the response onto it.
        """
        await seed_cap(session, EntityType.ORGANIZATION, ORG_ID, PeriodType.MONTHLY, "9000.00")
        await seed_usage(session, EntityType.ORGANIZATION, ORG_ID, PeriodType.MONTHLY, "777.00")
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "100.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "10.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget", params={"entity_type": "org", "entity_id": ORG_ID})

        assert response.status_code == 200
        body = response.json()
        assert body["entity_type"] == "user", f"response switched to entity_type={body['entity_type']!r}; the entity_type param was honoured"
        assert "777" not in response.text

    async def test_another_tenants_ledger_is_never_read(self, session, caller_user_row):
        """T4 — cross-tenant isolation holds even on identical entity ids.

        Same ``entity_id`` in a different ``org_id``. The org partition is part of
        the read predicate, so the foreign row must be invisible; if ``org_id``
        were dropped from the filter this returns the other tenant's cap.

        ``org_id`` is the LEADING index column, so dropping it costs the index
        seek entirely and the scan falls back to rowid (insertion) order — which
        is why the foreign rows are seeded FIRST here rather than relying on a
        sort key. See the decoy-ordering note at the top of this module.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "4242.00", org_id="org-other-tenant")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "4242.00", org_id="org-other-tenant")
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "100.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "10.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert response.status_code == 200
        assert "4242" not in response.text, "another tenant's figures leaked; org_id is missing from the read predicate"


# ===========================================================================
# FR-1.1 — all three calendar periods
# ===========================================================================


class TestAllThreePeriods:
    """``get_budget_status_for_headers`` is MONTHLY-only (``:1264``). This is the gap."""

    @pytest.mark.parametrize(
        ("period", "cap", "spend"),
        [
            (PeriodType.DAILY, "10.00", "3.00"),
            (PeriodType.WEEKLY, "50.00", "20.00"),
            (PeriodType.MONTHLY, "200.00", "150.00"),
        ],
    )
    async def test_each_period_returns_its_own_figures(self, session, caller_user_row, period, cap, spend):
        """T5 — daily, weekly and monthly each report their own period (FR-1.1).

        All three are seeded at once with distinct figures, so an implementation
        that ignored ``period_type`` and always read the monthly row would return
        the wrong numbers for two of the three cases rather than passing by
        accident.
        """
        for seeded, amount_cap, amount_spend in [
            (PeriodType.DAILY, "10.00", "3.00"),
            (PeriodType.WEEKLY, "50.00", "20.00"),
            (PeriodType.MONTHLY, "200.00", "150.00"),
        ]:
            await seed_cap(session, EntityType.USER, CALLER_SUB, seeded, amount_cap)
            await seed_usage(session, EntityType.USER, CALLER_SUB, seeded, amount_spend)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget", params={"period_type": period.value})

        assert response.status_code == 200
        body = response.json()
        assert body["period"]["period_type"] == period.value
        assert body["cap_usd"] == cap
        assert Decimal(body["spend_usd"]) == Decimal(spend)

    async def test_monthly_is_the_default_period(self, session, caller_user_row):
        """T6 — omitting ``period_type`` reports the monthly window."""
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "200.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert response.json()["period"]["period_type"] == "monthly"

    async def test_period_bounds_match_the_usage_lookup_key(self, session, caller_user_row):
        """T6b — the reported ``period_start`` is the one used in the query.

        A client (and U-3's drill-down) reproduces the read from these bounds, so
        a response describing a different window than it queried would be
        undetectably wrong.
        """
        from src.budget.utils import get_period_start_end

        expected_start, expected_end = get_period_start_end(PeriodType.WEEKLY)
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.WEEKLY, "50.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget", params={"period_type": "weekly"})

        period = response.json()["period"]
        assert period["period_start"] == expected_start.isoformat()
        assert period["period_end"] == expected_end.isoformat()
        assert period["resets_in_days"] == (expected_end - date.today()).days


# ===========================================================================
# FR-1.3 — 5-filter spend parity against enforcement
# ===========================================================================


class TestSpendParityWithEnforcement:
    """The endpoint's figure must equal the one enforcement actually compares against."""

    async def test_spend_equals_the_figure_check_entity_budget_uses(self, session, caller_user_row):
        """T7 — parity asserted against ``_check_entity_budget``, not a literal (FR-1.3).

        Decoy rows are seeded across other entity types for the same period, all
        keyed on the caller's own id. Their presence is the whole test: a read
        missing the ``entity_type`` filter picks one of them up, which is #4328's
        defect class. The expected value is taken from ``_check_entity_budget``'s
        own reservation target — never hand-computed, and never a fixed
        over-report factor, because the divergence is data-dependent.

        **The decoy entity types are not arbitrary.** Both ``organization`` and
        ``root_user`` sort before ``user``, so a read missing the ``entity_type``
        filter returns one of them rather than the caller's own row. Picking two
        decoys that happened to sort *after* ``user`` would leave that mutation
        undetected and this test green — see the decoy-ordering note at the top of
        this module. Verified by mutation.
        """
        await seed_usage(session, EntityType.ROOT_USER, CALLER_SUB, PeriodType.MONTHLY, "264.600000")
        await seed_usage(session, EntityType.ORGANIZATION, CALLER_SUB, PeriodType.MONTHLY, "412.804200")
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "500.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "148.204200")

        service = BudgetEnforcementService(db_session=session)
        _, target = await service._check_entity_budget(
            session,
            EntityType.USER,
            CALLER_SUB,
            PeriodType.MONTHLY,
            Decimal("0"),
            ORG_ID,
        )
        # `headroom = cap - settled`, so the settled figure enforcement used is
        # recoverable exactly. Reading it from enforcement rather than restating
        # it is what makes this a parity assertion.
        enforcement_spend = target.headroom_usd and (Decimal("500.00") - target.headroom_usd)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert Decimal(response.json()["spend_usd"]) == enforcement_spend

    async def test_spend_is_not_summed_across_entity_types(self, session, caller_user_row):
        """T8 — the over-report direction, asserted negatively (FR-1.6).

        Guards the specific failure the issue names: summing across entity types
        raises a false "over budget" alarm and makes the screen contradict
        enforcement. ``root_user`` as the decoy entity type sorts before ``user``,
        per the decoy-ordering note at the top of this module.
        """
        await seed_usage(session, EntityType.ROOT_USER, CALLER_SUB, PeriodType.MONTHLY, "264.60")
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "500.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "148.20")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert Decimal(response.json()["spend_usd"]) != Decimal("412.80"), "spend is the sum across entity types — the entity_type filter is missing"

    async def test_spend_from_a_different_period_start_is_excluded(self, session, caller_user_row):
        """T9 — the ``period_start`` filter, exercised.

        A usage row for last month must not appear in this month's figure. Without
        that filter the endpoint reports lifetime spend against a monthly cap. The
        stale row is dated 2020 so it sorts before the current window on the
        ``period_start`` index column, making the dropped filter observable — see
        the decoy-ordering note at the top of this module.
        """
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "888.00", period_start=date(2020, 1, 1))
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "500.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "11.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert Decimal(response.json()["spend_usd"]) == Decimal("11.00")

    async def test_spend_from_a_different_period_type_is_excluded(self, session, caller_user_row):
        """T9b — the ``period_type`` filter, exercised.

        The decoy is a ``daily`` usage row pinned to the *monthly* window's
        ``period_start``. That collision is what makes the filter observable at
        all: on most dates the two period types have different starts, so the
        ``period_start`` filter alone masks a missing ``period_type`` filter and
        the predicate looks correct while it is not. Rows in this shape are
        ordinary — the tracker writes one per period type per settlement — so this
        is a real read, not a contrived one.

        Verified by mutation: removing ``BudgetUsage.period_type`` from the
        predicate fails only this test.
        """
        from src.budget.utils import get_period_start_end

        monthly_start, _ = get_period_start_end(PeriodType.MONTHLY)
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.DAILY, "777.00", period_start=monthly_start)
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "500.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "22.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert Decimal(response.json()["spend_usd"]) == Decimal("22.00"), (
            "a daily usage row leaked into the monthly figure; the period_type filter is missing"
        )

    async def test_a_cap_on_another_entity_type_is_not_read_as_the_callers(self, session, caller_user_row):
        """T9c — the ``entity_type`` filter on the CAP read, exercised.

        The spend-side filters have their own tests above; this is the same
        predicate on ``budget_configs``. The decoy is a ``$1`` cap keyed on the
        caller's own id under a *different* entity type — seeded first, so a read
        missing ``entity_type`` picks it up and reports a $1 ceiling for a user
        whose real cap is $500.

        Rows in this shape are ordinary: the same principal legitimately holds
        caps under more than one entity type (a person's ``user`` line and their
        ``root_user`` cloud line), which is exactly why the filter is load-bearing
        rather than incidental.
        """
        await seed_cap(session, EntityType.AGENT, CALLER_SUB, PeriodType.MONTHLY, "1.00")
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "500.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "5.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["cap_usd"] == "500.00", "a cap on a different entity type was read as the caller's; the entity_type filter is missing"


# ===========================================================================
# FR-1.4 — the effective (clamped) cap
# ===========================================================================


class TestEffectiveCapClamp:
    """Render the clamped cap, never the raw ``budget_configs`` row."""

    async def test_configured_cap_above_the_platform_ceiling_is_clamped(self, session, caller_user_row):
        """T10 — $900 row with a $500 platform ceiling reports $500 (FR-1.4).

        The clamp is the security property: tenant admins can write their own
        ``budget_configs`` rows, so without ``min()`` raising your own cap would be
        self-service — the same reasoning as ``_resolve_scope_cap``.

        A real settings object is mutated with ``object.__setattr__``, not a
        MagicMock: a fully-patched config would assert a guarantee it never
        exercised.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "900.00")

        app = build_app(session, caller_context())
        object.__setattr__(budget_config, "budget_period_cap_usd", Decimal("500.00"))
        try:
            async with client_for(app) as client:
                response = await client.get("/me/budget")
        finally:
            object.__setattr__(budget_config, "budget_period_cap_usd", None)

        assert response.json()["cap_usd"] == "500.00"

    async def test_configured_cap_below_the_ceiling_is_honoured(self, session, caller_user_row):
        """T11 — a tenant may tighten its own ceiling.

        The other half of ``min()``: clamping must not become "always the platform
        number", which would ignore every tenant's deliberate lower cap.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "120.00")

        app = build_app(session, caller_context())
        object.__setattr__(budget_config, "budget_period_cap_usd", Decimal("500.00"))
        try:
            async with client_for(app) as client:
                response = await client.get("/me/budget")
        finally:
            object.__setattr__(budget_config, "budget_period_cap_usd", None)

        assert response.json()["cap_usd"] == "120.00"

    async def test_default_config_reports_the_cap_enforcement_honours(self, session, caller_user_row):
        """T12 — with the ceiling unset (the shipped default) there is NO divergence.

        This is why ``budget_period_cap_usd`` ships as ``None``: calendar
        enforcement compares against the raw row, so a clamp active by default
        would make the screen advertise a different cap than the enforcer applies
        — the exact screen-vs-enforcer disagreement this EPIC exists to remove.
        """
        assert budget_config.budget_period_cap_usd is None, "the platform calendar ceiling must ship unset"
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "900.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert response.json()["cap_usd"] == "900.00"


# ===========================================================================
# FR-1.5 — "no cap" is not "$0 cap"
# ===========================================================================


class TestUncappedIsNotZeroCap:
    """Conflating the two shows an uncapped user as exhausted, or a $0 user as unlimited."""

    async def test_no_config_row_is_uncapped_with_null_cap(self, session, caller_user_row):
        """T13 — no row ⇒ ``cap_usd: null`` + ``cap_status: "uncapped"`` (FR-1.5).

        Spend is still reported: it is a real figure and useful on its own. Every
        cap-derived field is null, so nothing can be mistaken for a ceiling.
        """
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "42.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        assert body["cap_status"] == "uncapped"
        assert body["cap_usd"] is None
        assert body["remaining_usd"] is None
        assert body["utilization_pct"] is None
        assert body["band"] is None
        assert Decimal(body["spend_usd"]) == Decimal("42.00")

    async def test_zero_cap_row_is_capped_at_zero(self, session, caller_user_row):
        """T14 — a ``$0`` row ⇒ ``capped`` at ``"0.00"``, band ``exceeded`` (FR-1.5).

        A $0 cap is a deliberate hard stop: no request with any cost can pass.
        ``utilization_pct`` is null because the ratio is undefined at a zero
        denominator — reporting ``0.0`` would read as "plenty of room", which is
        why ``calculate_budget_utilization`` (which returns 0.0 here) is not used
        on this path.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "0.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        assert body["cap_status"] == "capped"
        assert body["cap_usd"] == "0.00"
        assert body["band"] == "exceeded"
        assert body["utilization_pct"] is None

    async def test_the_two_responses_differ(self, session, engine, caller_user_row):
        """T15 — asserted as a direct inequality, per the issue's gate.

        Two separate sessions so the "no row" case is genuinely rowless rather
        than a deleted row in a dirty session.
        """
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as uncapped_session:
            app = build_app(uncapped_session, caller_context())
            async with client_for(app) as client:
                uncapped = (await client.get("/me/budget")).json()

        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "0.00")
        async with factory() as zero_session:
            app = build_app(zero_session, caller_context())
            async with client_for(app) as client:
                zero_cap = (await client.get("/me/budget")).json()

        assert uncapped != zero_cap
        assert (uncapped["cap_status"], uncapped["cap_usd"]) == ("uncapped", None)
        assert (zero_cap["cap_status"], zero_cap["cap_usd"]) == ("capped", "0.00")


# ===========================================================================
# FR-1.7 — a backend failure is never a 200 with zeroes
# ===========================================================================


class TestBackendFailureIsNotZero:
    async def test_db_error_returns_503_not_zeroed_budget(self, session, caller_user_row):
        """T16 — a ledger read failure surfaces as ``503`` (FR-1.7).

        ``get_budget_status_for_headers`` returns ``{}`` for both "no budget" and
        "DB error" (``:1319-1323``), which is why its logic is reimplemented here
        rather than called. A 200 with zeroes during an outage renders as "you
        have spent nothing" — silent, and trusted.
        """
        failing = MagicMock(spec=AsyncSession)
        failing.scalar = AsyncMock(side_effect=OperationalError("SELECT 1", {}, Exception("connection reset")))

        app = build_app(failing, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert response.status_code == 503
        body = response.json()
        assert "spend_usd" not in body
        assert "0.00" not in str(body.get("detail", "")), "the 503 body must not carry a zero figure that a client could render"

    async def test_identity_resolution_failure_does_not_zero_the_budget(self, session, caller_user_row):
        """T16b — a fault while resolving identity is also a 503, not a $0.

        ``resolve_canonical_user_id`` swallows ``SQLAlchemyError`` internally and
        degrades to the raw sub, so this asserts the *route* still reports a real
        failure honestly rather than serving figures built on a degraded identity
        silently. The degraded-but-successful case is T18.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "100.00")

        app = build_app(session, caller_context())
        with patch("src.budget.me_routes.resolve_canonical_user_id", side_effect=OperationalError("SELECT 1", {}, Exception("boom"))):
            async with client_for(app) as client:
                response = await client.get("/me/budget")

        assert response.status_code == 503


# ===========================================================================
# FR-3.6 — non-calendar period types are 422, never 500
# ===========================================================================


class TestNonCalendarPeriodTypes:
    @pytest.mark.parametrize("bad_period", ["run", "chain"])
    async def test_run_and_chain_are_422(self, session, caller_user_row, bad_period):
        """T17 — ``period_type=run|chain`` ⇒ ``422``, not a 500 (FR-3.6).

        Run/chain caps are lifetime-scoped, so ``get_period_start_end``
        deliberately raises ``ValueError`` for them. Unguarded, that raise is a
        500 — a server error for what is a bad request.
        """
        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget", params={"period_type": bad_period})

        assert response.status_code == 422, (
            f"period_type={bad_period} returned {response.status_code}; a non-calendar period is a bad request, not a server error"
        )

    async def test_the_second_guard_catches_a_widened_annotation(self, session, caller_user_row):
        """T17b — the defence-in-depth layer, exercised directly.

        The route's ``Literal`` rejects these at the HTTP boundary, so
        ``_resolve_period_bounds``' own check is unreachable through the API. It
        exists so that widening the annotation without teaching
        ``get_period_start_end`` about the new value yields a 422 rather than a
        500 — tested at the function level, since that is the only way to reach it.
        """
        from fastapi import HTTPException

        from src.budget.me_routes import _resolve_period_bounds

        with pytest.raises(HTTPException) as exc:
            _resolve_period_bounds("run")
        assert exc.value.status_code == 422

    async def test_an_unknown_period_type_is_422(self, session, caller_user_row):
        """T17c — garbage input is a bad request too, not a 500."""
        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget", params={"period_type": "fortnightly"})

        assert response.status_code == 422


# ===========================================================================
# Identity resolution — "unresolvable" is not "$0"
# ===========================================================================


class TestIdentityResolution:
    async def test_unresolvable_identity_is_reported_not_rendered_as_zero(self, session):
        """T18 — the raw-sub fallback must not produce a false $0 (api-contract §0).

        No ``users`` row is seeded, so ``resolve_canonical_user_id`` falls back to
        the raw Cognito sub (``resolver.py:50-62``). Querying the ``root_user``
        ledger with a sub finds nothing and would render $0 cloud spend — the
        exact "screen says $0" failure the EPIC warns about. The response must say
        ``unresolved`` instead.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "100.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        assert response.status_code == 200
        assert response.json()["identity_status"] == "unresolved"

    async def test_resolved_identity_reads_the_root_user_ledger(self, session, caller_user_row):
        """T19 — with a ``users`` row, the cloud-agent line is in scope.

        The ``root_user`` cap is seeded under the canonical ``users.id`` and made
        the binding line. Keyed under the wrong namespace it would find no row and
        the endpoint would report the user line instead — the T8 namespace trap
        from ``test_root_human_envelope.py``, one level over.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "200.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "148.20")
        await seed_cap(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, "300.00")
        await seed_usage(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, "264.60")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        assert body["identity_status"] == "resolved"
        assert body["entity_type"] == "root_user", "the root_user line did not bind; it is likely keyed under the wrong id namespace"
        assert Decimal(body["remaining_usd"]) == Decimal("35.40")

    async def test_service_account_identity_is_not_applicable(self, session):
        """T20 — a service account has no ``users`` row by design.

        Reporting ``unresolved`` for it would be a false alarm on the normal path
        for every agent caller.
        """
        await seed_cap(session, EntityType.SERVICE_ACCOUNT, "sa-4397", PeriodType.MONTHLY, "50.00")

        app = build_app(session, caller_context(user_id="sa-4397", account_type="service"))
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        body = response.json()
        assert body["identity_status"] == "not_applicable"
        assert body["entity_type"] == "service_account"


# ===========================================================================
# Binding-line selection
# ===========================================================================


class TestBindingLineSelection:
    async def test_lowest_remaining_line_binds(self, session, caller_user_row):
        """T21 — the binding line is the one that stops the caller first.

        The api-contract's worked example: user $200 cap / $148.20 spent
        ($51.80 left) vs root_user $300 / $264.60 ($35.40 left). The root_user
        line binds. Same selection ``get_budget_status_for_headers`` makes.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "200.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "148.20")
        await seed_cap(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, "300.00")
        await seed_usage(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, "264.60")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["entity_type"] == "root_user"
        assert Decimal(body["remaining_usd"]) == Decimal("35.40")

    async def test_headline_is_never_the_sum_of_the_two_lines(self, session, caller_user_row):
        """T22 — the negative that guards the EPIC's R3 gate.

        A summed headline ($412.80) exists in no ledger row and is enforced by no
        cap, so the screen would say "exhausted" while enforcement stopped
        nothing. Asserted negatively because that is the failure mode.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "200.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "148.20")
        await seed_cap(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, "300.00")
        await seed_usage(session, EntityType.ROOT_USER, CALLER_CANONICAL_ID, PeriodType.MONTHLY, "264.60")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert Decimal(body["spend_usd"]) != Decimal("412.80")

    async def test_an_uncapped_line_cannot_bind(self, session, caller_user_row):
        """T23 — a line with spend but no cap is not a candidate.

        The team line here has far more spend than the user line but no cap, so it
        can never stop anyone. Treating a missing cap as $0 would make it bind
        instantly and report a phantom exceeded budget.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "200.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "10.00")
        await seed_usage(session, EntityType.TEAM, "team-4397", PeriodType.MONTHLY, "5000.00")

        app = build_app(session, caller_context(team_id="team-4397"))
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["entity_type"] == "user"
        assert body["cap_status"] == "capped"

    async def test_team_and_department_caps_are_in_scope(self, session, caller_user_row):
        """T24 — the hierarchy read matches the one enforcement checks.

        A department cap that binds must be reported; if the read consulted a
        narrower hierarchy than enforcement, the user would be stopped by a cap
        the screen never showed.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "200.00")
        await seed_cap(session, EntityType.DEPARTMENT, "dept-4397", PeriodType.MONTHLY, "100.00")
        await seed_usage(session, EntityType.DEPARTMENT, "dept-4397", PeriodType.MONTHLY, "99.00")

        app = build_app(session, caller_context(team_id="team-4397", department_id="dept-4397"))
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["entity_type"] == "department"
        assert Decimal(body["remaining_usd"]) == Decimal("1.00")

    async def test_empty_team_and_department_ids_are_not_queried(self, session, caller_user_row):
        """T25 — a blank id must never become a ledger key.

        ``team_id``/``department_id`` default to ``""``. A row keyed on ``""``
        would be one bogus shared line for every caller in the tenant, so blank
        ids are skipped rather than queried.
        """
        await seed_cap(session, EntityType.TEAM, "", PeriodType.MONTHLY, "1.00")
        await seed_usage(session, EntityType.TEAM, "", PeriodType.MONTHLY, "1.00")
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "200.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["entity_type"] == "user", "an empty-id row bound the response; blank ids must be skipped"


# ===========================================================================
# Bands, precision, and overage
# ===========================================================================


class TestBandsAndPrecision:
    @pytest.mark.parametrize(
        ("spend", "expected_band"),
        [
            ("79.00", "none"),
            ("85.00", "warning"),
            ("97.00", "critical"),
            ("101.00", "exceeded"),
        ],
    )
    async def test_bands_come_from_the_server_side_thresholds(self, session, caller_user_row, spend, expected_band):
        """T26 — 80/95 from ``config.py:147-148``, not a new constant (FR-5.3).

        The frontend today hardcodes a *different* band (50/80 in
        ``BudgetManagement.tsx``); these thresholds are the server's, and 95% is a
        critical *warning* — enforcement blocks at >= 100%.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "100.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, spend)

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["band"] == expected_band

    async def test_money_fields_are_strings_not_floats(self, session, caller_user_row):
        """T27 — money is a JSON string throughout.

        Asserted against the raw response TEXT, not the parsed body: ``json.loads``
        would happily turn a JSON number into a Python float and the assertion
        would pass on a broken wire format. A float loses the sub-cent precision
        ``NUMERIC(14,6)`` holds — the class of defect that made haiku traffic
        accrue real spend against a $0.00 accumulator (migration 030).
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "100.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "12.345678")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            response = await client.get("/me/budget")

        raw = response.text
        for field in ("cap_usd", "spend_usd", "remaining_usd"):
            assert re.search(rf'"{field}":\s*"', raw), f"{field} is not a JSON string in the raw response: {raw}"

        body = response.json()
        for field in ("cap_usd", "spend_usd", "remaining_usd"):
            assert isinstance(body[field], str)
        # utilization_pct is deliberately a number — it is a ratio, not money.
        assert isinstance(body["utilization_pct"], float)

    async def test_sub_cent_spend_precision_survives(self, session, caller_user_row):
        """T28 — 6dp is preserved, not rounded to cents.

        ``budget_usage.total_cost_usd`` is ``NUMERIC(14,6)``; truncating to 2dp
        here would report a real sub-cent balance as ``$0.00``.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "100.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "0.001234")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert Decimal(body["spend_usd"]) == Decimal("0.001234")

    async def test_money_is_never_scientific_notation(self, session, caller_user_row):
        """T29 — a zero at 6dp must not serialise as ``0E-6``.

        ``Decimal("0").quantize(...)`` formatted with ``str`` yields ``0E-6``,
        which breaks every client parsing this with a plain decimal reader. The
        ``:f`` format in ``format_money`` is what prevents it.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "100.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert "E" not in body["spend_usd"].upper()
        assert Decimal(body["spend_usd"]) == Decimal("0")

    async def test_remaining_is_negative_when_spend_passed_the_cap(self, session, caller_user_row):
        """T30 — overage is shown, not clamped to zero.

        The response-header path clamps at zero (``enforcement_service.py:1314``),
        which hides an overage behind a flat "$0.00 left". Wrong for a read
        surface whose purpose is showing the true position — settled spend can
        exceed a cap that was lowered after the fact, or that the async tracker
        settled after the requests were admitted.
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "100.00")
        await seed_usage(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "130.00")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert Decimal(body["remaining_usd"]) == Decimal("-30.00")
        assert body["band"] == "exceeded"

    async def test_enforcement_mode_is_reported_from_the_binding_row(self, session, caller_user_row):
        """T31 — the field a client must consult before promising a stop.

        Enforcement is in shadow mode for run/chain caps and a ``soft`` cap never
        blocks, so a UI that said "your spend will be stopped" without reading
        this would be lying (FR-5.5).
        """
        await seed_cap(session, EntityType.USER, CALLER_SUB, PeriodType.MONTHLY, "100.00", enforcement_mode="soft")

        app = build_app(session, caller_context())
        async with client_for(app) as client:
            body = (await client.get("/me/budget")).json()

        assert body["enforcement_mode"] == "soft"


# ===========================================================================
# Static gates — NFR-1 / FR-1.6
# ===========================================================================

_SRC = Path(__file__).resolve().parents[2] / "src"


class TestStaticGates:
    """Asserted in the suite, not just at review, so they cannot regress silently."""

    def test_forbidden_overview_helper_is_never_called(self):
        """T32 — ``get_organization_budget_overview`` is never called (FR-1.6).

        It sums without filtering by ``entity_type``, over-reporting 2x-4x
        (#4328), and ``KeyError``-500s on ``agent``/``run``/``chain``/``root_user``
        rows that exist in production.

        Comments and docstrings are stripped before matching, deliberately: the
        modules *document* why this helper is avoided, and a raw substring check
        would fail on that documentation and push the next person to delete the
        explanation to make the test pass. Calls and imports are what matter.
        """
        import ast

        for name in ("me_routes.py", "schemas.py"):
            tree = ast.parse((_SRC / "budget" / name).read_text())
            referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            referenced |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    referenced |= {alias.name for alias in node.names}
            assert "get_organization_budget_overview" not in referenced, f"{name} calls or imports the unfiltered-SUM overview helper"

    def test_no_sql_sum_in_the_new_read_path(self):
        """T33 — no ``SUM`` at all, filtered or otherwise (FR-1.6).

        Every figure is a single 5-filter row read against the table's unique
        constraint, so an aggregate appearing here is a change of approach that
        should be a deliberate, reviewed decision.
        """
        source = (_SRC / "budget" / "me_routes.py").read_text()
        assert not re.search(r"\bfunc\.sum\b|\bSUM\s*\(", source, re.IGNORECASE), (
            "me_routes.py contains a SQL SUM; every figure must be a single 5-filter row read"
        )

    def test_the_idor_prone_router_is_untouched(self):
        """T34 — this unit adds no route to ``src/budget/routes.py`` (NFR-1).

        A route there would inherit #4384's unscoped ``entity_type``/``entity_id``.
        The issue's gate is a ``git diff`` being empty; asserted here as a route
        count so it holds without a git invocation in CI.
        """
        from src.budget.routes import router as legacy_router

        paths = {getattr(route, "path", "") for route in legacy_router.routes}
        assert not any("/me" in path for path in paths), f"an own-scope route was added to the IDOR-prone router: {sorted(paths)}"

    def test_no_writes_in_the_read_path(self):
        """T35 — read-only (NFR-2).

        No ``add``/``commit``/``flush``/``delete``/``insert``/``update`` call. A
        write appearing on a read surface is how a "harmless" endpoint starts
        mutating caps.

        Matched on the AST's called-attribute names rather than on source text, so
        the module's prose (which discusses what it does *not* write) cannot trip
        it, and ``ruff format`` reflowing a line cannot hide a real call.
        """
        import ast

        tree = ast.parse((_SRC / "budget" / "me_routes.py").read_text())
        called = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        called |= {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}

        forbidden = {"add", "add_all", "commit", "flush", "delete", "insert", "update", "merge", "execute_write"}
        assert not (called & forbidden), f"me_routes.py calls {sorted(called & forbidden)}; this unit is read-only (NFR-2)"


class TestRouterRegistration:
    def test_router_is_registered_in_the_app(self):
        """T36 — an unregistered router is a 404 for an authenticated user."""
        from src.app import UNIT_MODULES

        assert "src.budget.me_routes" in UNIT_MODULES

    def test_route_is_mounted_without_an_api_prefix(self):
        """T37 — CloudFront strips the first ``/api`` (issue #4330).

        The browser calls ``/api/me/budget``; the origin must serve
        ``/me/budget``. A router declaring ``/api`` would 404 for a fully
        authenticated operator.
        """
        paths = {getattr(route, "path", "") for route in me_budget_router.routes}
        assert "/me/budget" in paths
        assert not any(path.startswith("/api") for path in paths)


class TestConfigLever:
    def test_platform_period_ceiling_ships_unset(self):
        """T38 — the default must stay ``None``.

        While unset, the reported cap equals the raw row calendar enforcement
        honours, so screen and enforcer agree. Setting it introduces a real
        (honest, under-promising) divergence — see the comment on the setting.
        """
        assert BudgetSettings().budget_period_cap_usd is None
