"""Run drill-down API — Issue #4400 (U-3 of EPIC #4324).

``GET /me/budget/runs`` answers "what spent it" for the signed-in caller. Two
properties carry the whole unit and every test below serves one of them:

1. **Scope is structural.** The endpoint takes no ``user_id``/``entity_id`` param
   and the lineage query is *partitioned* on the caller's own canonical id, so
   another member's runs are not filtered out — they are never read.
2. **Cost is three-valued.** A lineage row with no ``usage_logs`` row is
   ``unknown``, never ``$0.00``, and a subtotal missing an unmeasured
   contribution is flagged ``partial`` because it is a lower bound.

**Authz negatives come first** (NFR-3), before any happy path — they are the
tests that would catch #4384's IDOR being re-created on this surface.

Harness notes:

* ``FakeActivityService`` below is a real *behavioural* stand-in for the DynamoDB
  query, not a ``MagicMock`` returning a canned page. It partitions on
  ``user_id``/``root_human_id``, applies the ``arrived_at`` window
  **lexicographically** exactly as DynamoDB's ``Key(...).between`` does, and maps
  rows through the **real** ``ActivityService._map_item``. That matters three
  times over: a ``MagicMock`` would assert the endpoint's period bounds without
  ever exercising them (the #4046 trap), the lexicographic comparison is what
  makes T7's last-day case a real test rather than a tautology, and using the
  real mapper pins the ``invocation_id``-from-``event_id`` fallback (#1756) that
  the cost join depends on.
* Cost comes from real ``UsageLog`` rows in in-memory SQLite via the real
  ``get_cost_by_run_ids``, so "no usage row" is genuine row-absence rather than a
  mocked ``None``. That is the only way to test the branch that matters.
* Money is compared against ``format_money`` output, never a hand-written string,
  so the 6dp precision contract cannot be restated wrongly in a test.
"""

import ast
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.activity.routes import get_activity_service
from src.activity.schemas import InvocationListResponse
from src.activity.service import NON_TRIGGERING_STATUSES, ActivityService
from src.auth.dependencies import get_current_user
from src.budget.me_routes import router as me_budget_router
from src.budget.schemas import SPEND_PLACES, format_money
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import User
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext

ORG_ID = "org-4400"

# The caller. Their Cognito sub is what the token carries; their canonical
# `users.id` is what the lineage partition and the `root_user` ledger are keyed
# by (#4300). Keeping them distinct is what makes T13 a real test.
CALLER_SUB = "sub-caller-4400"
CALLER_CANONICAL_ID = "11111111-1111-4111-8111-111111111111"

# A colleague in the same org, and a member of a different tenant. Both are
# seeded with runs whose ids and costs are unmistakably theirs, so an endpoint
# leaking them fails on the value rather than coincidentally matching.
OTHER_SUB = "sub-colleague-4400"
OTHER_CANONICAL_ID = "22222222-2222-4222-8222-222222222222"
OTHER_RUN_ID = "run-colleague-must-not-appear"

FOREIGN_ORG_ID = "org-other-tenant-4400"
FOREIGN_CANONICAL_ID = "33333333-3333-4333-8333-333333333333"
FOREIGN_RUN_ID = "run-foreign-tenant-must-not-appear"


# ---------------------------------------------------------------------------
# Lineage harness
# ---------------------------------------------------------------------------


def lineage_row(
    event_id: str,
    *,
    arrived_at: str,
    user_id: str | None = None,
    root_human_id: str | None = None,
    persona: str = "developer",
    status: str = "complete",
    correlation_id: str | None = None,
    run_id: str | None = None,
) -> dict:
    """One raw ``webhook-events`` item, in the shape DynamoDB actually stores.

    ``event_id`` and ``run_id`` are deliberately separate arguments, and default
    to *different* values: ``event_id`` is the agent-worker's message id and the
    join key into ``usage_logs.agent_run_id``, while the attribute literally named
    ``run_id`` is the KEDA job/pod name and matches no usage row at all. A harness
    that set them equal would make an endpoint joining on the wrong one pass —
    which is exactly the silent all-``$0.00`` bug the join-key guard in
    ``src/orchestration/cost.py`` exists to prevent.
    """
    return {
        "event_id": event_id,
        "arrived_at": arrived_at,
        "user_id": user_id,
        "root_human_id": root_human_id,
        "persona": persona,
        "status": status,
        "correlation_id": correlation_id,
        # A realistic KEDA job name — never equal to `event_id`.
        "run_id": run_id or f"agent-gateway-worker-{event_id[-5:]}",
        "channel": "github",
        "topic": f"topic for {event_id}",
    }


class FakeActivityService:
    """A behavioural stand-in for the DynamoDB lineage query.

    Reproduces the three properties of ``ActivityService._execute_query`` this
    endpoint depends on, and nothing else:

    * **Partitioning** — a row is visible to ``query_by_user(user_id=X)`` only if
      its ``user_id`` or ``root_human_id`` is ``X``. This is what makes the authz
      tests meaningful: a colleague's rows are in another partition, so they are
      unreachable rather than merely filtered.
    * **Lexicographic ``arrived_at`` window** — ``since <= arrived_at <= until``
      compared as *strings*, as ``Key("arrived_at").between`` does. A bare-date
      upper bound therefore drops the period's last day here just as it would in
      production, which is what gives T7 teeth.
    * **Non-triggering statuses excluded** by default, matching the real filter.

    Rows are mapped through the real ``ActivityService._map_item``, so the
    ``invocation_id``-from-``event_id`` fallback is the production one.
    """

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict] = []
        self.next_cursor: str | None = None

    def query_by_user(self, *, user_id: str, page_size: int = 20, last_key: str | None = None, since=None, until=None, **_) -> InvocationListResponse:
        self.calls.append({"user_id": user_id, "page_size": page_size, "last_key": last_key, "since": since, "until": until})

        selected = [
            row
            for row in self.rows
            if user_id in (row.get("user_id"), row.get("root_human_id"))
            and row.get("status") not in NON_TRIGGERING_STATUSES
            # String comparison on purpose — see the class docstring.
            and (since is None or row["arrived_at"] >= since)
            and (until is None or row["arrived_at"] <= until)
        ]
        selected.sort(key=lambda row: row["arrived_at"], reverse=True)
        items = [ActivityService._map_item(row) for row in selected[:page_size]]
        return InvocationListResponse(items=items, count=len(items), last_key=self.next_cursor)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
async def caller_user_row(session) -> None:
    """The ``users`` row that lets the Cognito sub resolve to a canonical id.

    Without it ``resolve_canonical_user_id`` hands back the raw sub, which is the
    ``identity_status="unresolved"`` path (T14) rather than a lookup this endpoint
    can trust. Every test wanting a resolved identity depends on this.
    """
    # `team_id` is NOT NULL on `users`; the token's team is a separate field.
    session.add(User(id=CALLER_CANONICAL_ID, cognito_sub=CALLER_SUB, email="caller@example.com", org_id=ORG_ID, team_id=""))
    session.add(User(id=OTHER_CANONICAL_ID, cognito_sub=OTHER_SUB, email="colleague@example.com", org_id=ORG_ID, team_id=""))
    await session.commit()


def caller_context(**overrides) -> TokenContext:
    defaults = {
        "user_id": CALLER_SUB,
        "org_id": ORG_ID,
        "team_id": "",
        "department_id": "",
        "account_type": "human",
        "expires_at": date(2099, 1, 1),
    }
    return TokenContext(**{**defaults, **overrides})


def build_app(session: AsyncSession, context: TokenContext | None = None, activity: FakeActivityService | None = None) -> FastAPI:
    """Mount the router alone, with auth, db and the lineage service overridden."""
    app = FastAPI()
    app.include_router(me_budget_router)

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_activity_service] = lambda: activity or FakeActivityService()
    if context is not None:
        app.dependency_overrides[get_current_user] = lambda: context
    return app


def client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def seed_cost(session: AsyncSession, agent_run_id: str, amount: str, *, org_id: str = ORG_ID, rows: int = 1) -> None:
    """Insert real ``usage_logs`` rows for a run.

    ``rows`` matters: the three-valued classification turns on the row COUNT, not
    the sum, so seeding a row with ``cost_usd=0`` is how ``none_incurred`` (a
    measured zero) is distinguished from ``unknown`` (no measurement).
    """
    per_row = Decimal(amount) / rows
    for index in range(rows):
        session.add(
            UsageLog(
                org_id=org_id,
                department_id="dept-1",
                team_id="team-1",
                user_id=f"worker-{agent_run_id}-{index}",
                model="anthropic.claude-sonnet-4",
                input_tokens=100,
                output_tokens=200,
                cost_usd=per_row,
                latency_ms=1200,
                status_code=200,
                agent_run_id=agent_run_id,
            )
        )
    await session.commit()


def money(amount: str) -> str:
    """The wire rendering of an amount, from the contract's own formatter."""
    return format_money(Decimal(amount), SPEND_PLACES)


# Today, and a run instant inside today — so every default-period (monthly) test
# selects its rows without hard-coding a month that would go stale.
TODAY = date.today()
TODAY_AT = f"{TODAY.isoformat()}T09:00:00Z"


# ===========================================================================
# Authz negatives — FIRST, per NFR-3
# ===========================================================================


class TestAuthorizationNegatives:
    """The tests that must exist before any happy path.

    #4384 is an open IDOR on ``src/budget/routes.py``, which reads
    ``entity_type``/``entity_id`` straight off the request. This unit's placement
    on the ``/me/*`` router exists to avoid inheriting it; these prove it did.
    """

    async def test_unauthenticated_request_is_rejected(self, session):
        """T1 — no token, no runs.

        ``get_current_user`` is deliberately NOT overridden, so the real
        dependency runs. Asserting "not 200" rather than a specific code: the
        point is that run ids and costs do not leak without a token.
        """
        app = build_app(session)  # no auth override
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        assert response.status_code in (401, 403), (
            f"unauthenticated request returned {response.status_code}; run history must not be readable without a token"
        )
        assert "subtotal" not in response.text

    async def test_another_members_runs_are_never_read(self, session, caller_user_row):
        """T2 — a colleague's runs are in a different partition, so they cannot appear.

        Both members' rows are in the SAME table and the same period; only the
        lineage partition separates them. An endpoint that queried on anything
        broader than the caller's own canonical id returns the colleague's run
        here and fails on the id, not merely on a count.
        """
        activity = FakeActivityService(
            [
                lineage_row("run-mine-1", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID),
                lineage_row(OTHER_RUN_ID, arrived_at=TODAY_AT, user_id=OTHER_CANONICAL_ID),
            ]
        )
        await seed_cost(session, "run-mine-1", "1.500000")
        await seed_cost(session, OTHER_RUN_ID, "999.000000")

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        assert response.status_code == 200
        body = response.json()
        assert [item["run_id"] for item in body["items"]] == ["run-mine-1"]
        assert OTHER_RUN_ID not in response.text, "a colleague's run id leaked into the caller's drill-down"
        assert "999" not in response.text, "a colleague's cost leaked into the caller's drill-down"
        # The query was partitioned on the caller's CANONICAL id, not their sub —
        # a sub-keyed query matches no lineage row and would render an empty list.
        assert activity.calls[0]["user_id"] == CALLER_CANONICAL_ID

    async def test_another_tenants_runs_are_never_read(self, session, caller_user_row):
        """T3 — cross-tenant isolation, with the foreign run carrying real cost rows.

        The foreign tenant's ``usage_logs`` rows exist and are reachable by the
        same Postgres session, so if the run ever reached ``items`` its cost would
        resolve and be reported. Nothing about it may appear.
        """
        activity = FakeActivityService(
            [
                lineage_row("run-mine-1", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID),
                lineage_row(FOREIGN_RUN_ID, arrived_at=TODAY_AT, user_id=FOREIGN_CANONICAL_ID),
            ]
        )
        await seed_cost(session, FOREIGN_RUN_ID, "4242.000000", org_id=FOREIGN_ORG_ID)

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        assert response.status_code == 200
        assert FOREIGN_RUN_ID not in response.text, "another tenant's run leaked"
        assert "4242" not in response.text, "another tenant's cost leaked"

    async def test_user_id_param_naming_another_user_is_not_honoured(self, session, caller_user_row):
        """T4 — ``?user_id=<colleague>`` returns the CALLER's own runs (FR-3.3).

        The param is not merely rejected, it does not exist: FastAPI ignores
        unknown query params, so the caller's own runs come back. Asserted on the
        returned ids rather than a status code, because a 200 that quietly
        switched partitions is the failure this guards.
        """
        activity = FakeActivityService(
            [
                lineage_row("run-mine-1", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID),
                lineage_row(OTHER_RUN_ID, arrived_at=TODAY_AT, user_id=OTHER_CANONICAL_ID),
            ]
        )

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get(
                "/me/budget/runs",
                params={"user_id": OTHER_CANONICAL_ID, "entity_id": OTHER_CANONICAL_ID, "entity_type": "user"},
            )

        assert response.status_code == 200
        assert [item["run_id"] for item in response.json()["items"]] == ["run-mine-1"]
        assert OTHER_RUN_ID not in response.text, "the user_id/entity_id param was honoured — this is #4384 re-created"
        assert activity.calls[0]["user_id"] == CALLER_CANONICAL_ID

    def test_the_endpoint_declares_no_scope_parameter(self):
        """T5 — the absence of a scope param is asserted, not just relied upon.

        T4 proves a scope param is not *honoured today*. This proves none is
        *declared*, so a future signature that adds ``user_id`` — where FastAPI
        would start binding it — fails here rather than silently becoming an IDOR.
        """
        import inspect

        from src.budget.me_routes import get_my_budget_runs

        params = set(inspect.signature(get_my_budget_runs).parameters)
        forbidden = {"user_id", "entity_id", "entity_type", "org_id", "team_id", "department_id"}
        assert not (params & forbidden), (
            f"get_my_budget_runs declares scope params {sorted(params & forbidden)}; identity must come only from the token"
        )


# ===========================================================================
# FR-3.1 — the period window
# ===========================================================================


class TestPeriodSelection:
    async def test_runs_outside_the_period_are_excluded(self, session, caller_user_row):
        """T6 — the window is real: an out-of-period run does not appear (FR-3.1).

        ``period_type=daily`` with an explicit ``period_start`` gives a
        single-day window, and the neighbouring days' runs must be absent. The
        harness applies the bounds the endpoint sent, so this fails if the
        endpoint sends no bounds at all.
        """
        activity = FakeActivityService(
            [
                lineage_row("run-day-before", arrived_at="2026-06-12T23:00:00Z", user_id=CALLER_CANONICAL_ID),
                lineage_row("run-in-window", arrived_at="2026-06-13T12:00:00Z", user_id=CALLER_CANONICAL_ID),
                lineage_row("run-day-after", arrived_at="2026-06-14T01:00:00Z", user_id=CALLER_CANONICAL_ID),
            ]
        )

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs", params={"period_type": "daily", "period_start": "2026-06-13"})

        assert response.status_code == 200
        body = response.json()
        assert [item["run_id"] for item in body["items"]] == ["run-in-window"]
        assert body["period"] == {
            "period_type": "daily",
            "period_start": "2026-06-13",
            "period_end": "2026-06-13",
            "resets_in_days": body["period"]["resets_in_days"],
        }

    async def test_a_run_on_the_periods_last_day_is_included(self, session, caller_user_row):
        """T7 — the upper bound covers the whole final day (#4390).

        ``arrived_at`` is compared lexicographically, so a bare-date upper bound
        of ``"2026-06-30"`` sorts BELOW ``"2026-06-30T22:00:00Z"`` and silently
        drops every run on the month's last day. The harness compares strings
        exactly as DynamoDB does, so this test genuinely exercises the widening
        rather than asserting it.
        """
        activity = FakeActivityService(
            [
                lineage_row("run-first-instant", arrived_at="2026-06-01T00:00:00Z", user_id=CALLER_CANONICAL_ID),
                lineage_row("run-last-day-late", arrived_at="2026-06-30T22:00:00Z", user_id=CALLER_CANONICAL_ID),
            ]
        )

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs", params={"period_type": "monthly", "period_start": "2026-06-15"})

        assert response.status_code == 200
        returned = {item["run_id"] for item in response.json()["items"]}
        assert returned == {"run-first-instant", "run-last-day-late"}, (
            f"expected both boundary runs, got {sorted(returned)} — the period bounds are not full-day instants (#4390)"
        )
        # The bounds themselves, so the failure names the cause and not just the symptom.
        assert activity.calls[0]["since"] == "2026-06-01T00:00:00Z"
        assert activity.calls[0]["until"] == "2026-06-30T23:59:59.999Z"

    async def test_a_mid_period_period_start_is_normalised(self, session, caller_user_row):
        """T8 — a Wednesday sent as a weekly ``period_start`` selects Mon–Sun.

        The client is not trusted to have sent the exact first day. Without
        normalisation the run list and ``GET /me/budget``'s settled figure would
        describe different windows, and the drill-down would appear not to add up.
        """
        app = build_app(session, caller_context(), FakeActivityService())
        async with client_for(app) as client:
            # 2026-06-17 is a Wednesday.
            response = await client.get("/me/budget/runs", params={"period_type": "weekly", "period_start": "2026-06-17"})

        assert response.status_code == 200
        assert response.json()["period"]["period_start"] == "2026-06-15"  # Monday
        assert response.json()["period"]["period_end"] == "2026-06-21"  # Sunday

    async def test_monthly_is_the_default_period(self, session, caller_user_row):
        app = build_app(session, caller_context(), FakeActivityService())
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        assert response.status_code == 200
        assert response.json()["period"]["period_type"] == "monthly"
        assert response.json()["period"]["period_start"] == TODAY.replace(day=1).isoformat()


# ===========================================================================
# FR-3.2 — chain inclusivity
# ===========================================================================


class TestChainInclusivity:
    async def test_a_whole_chain_appears_under_the_initiating_caller(self, session, caller_user_row):
        """T9 — three sub-agents under one ``correlation_id`` all appear (FR-3.2).

        The sub-agents' rows carry a *worker* ``user_id``, not the caller's, and
        are attributed to them only through ``root_human_id`` (#3705). This is the
        case the EPIC exists for: a fan-out whose cost lands nowhere visible
        because each leg looks like it belongs to a machine.
        """
        chain = "corr-fanout-4400"
        activity = FakeActivityService(
            [
                lineage_row("run-root", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID, root_human_id=CALLER_CANONICAL_ID, correlation_id=chain),
                lineage_row("run-sub-1", arrived_at=TODAY_AT, user_id="worker-identity-a", root_human_id=CALLER_CANONICAL_ID, correlation_id=chain),
                lineage_row("run-sub-2", arrived_at=TODAY_AT, user_id="worker-identity-b", root_human_id=CALLER_CANONICAL_ID, correlation_id=chain),
                lineage_row("run-sub-3", arrived_at=TODAY_AT, user_id="worker-identity-c", root_human_id=CALLER_CANONICAL_ID, correlation_id=chain),
            ]
        )
        for run_id, amount in [("run-root", "1.000000"), ("run-sub-1", "88.000000"), ("run-sub-2", "88.000000"), ("run-sub-3", "88.000000")]:
            await seed_cost(session, run_id, amount)

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        assert response.status_code == 200
        body = response.json()
        assert {item["run_id"] for item in body["items"]} == {"run-root", "run-sub-1", "run-sub-2", "run-sub-3"}
        assert {item["correlation_id"] for item in body["items"]} == {chain}
        # And the chain's cost is visible, not stranded on the worker identities.
        assert body["subtotal"]["amount_usd"] == money("265.000000")
        assert body["subtotal"]["partial"] is False

    async def test_every_run_is_attributed_to_the_cloud_line(self, session, caller_user_row):
        """T10 — ``attribution`` names the envelope line the run counts against.

        Hosted runs land on the caller's ``root_user`` (``cloud``) ledger. The
        caller's ``direct`` traffic has no run binding and writes no lineage row,
        so it cannot appear here — reporting some runs as ``direct`` would
        attribute cloud spend to the caller's own machine.
        """
        activity = FakeActivityService([lineage_row("run-mine-1", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID)])

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        assert [item["attribution"] for item in response.json()["items"]] == ["cloud"]


# ===========================================================================
# FR-3.4 / FR-3.5 — three-valued cost
# ===========================================================================


class TestThreeValuedCost:
    """The heart of the unit. ``$0.00`` and "we do not know" are different claims."""

    async def test_a_run_with_no_usage_row_is_unknown_not_zero(self, session, caller_user_row):
        """T11 — the branch this whole unit exists for (FR-3.4).

        The lineage row exists and the run demonstrably did work; the ledger
        simply has no row for it yet, because cost back-fill is asynchronous. That
        is the COMMON state of a recent run. It must render as ``unknown`` with a
        reason and **no amount** — never ``0``, and never a 500.
        """
        activity = FakeActivityService([lineage_row("run-no-cost-yet", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID, status="in_progress")])

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        assert response.status_code == 200, "a missing cost row must not fail the drill-down"
        cost = response.json()["items"][0]["cost"]
        assert cost["status"] == "unknown"
        assert cost["reason"] == "no_usage_rows"
        assert cost["amount_usd"] is None, f"an unknown cost carried an amount ({cost['amount_usd']!r}) — that is how absence becomes $0.00"

    async def test_a_run_with_zero_cost_rows_is_a_measured_zero(self, session, caller_user_row):
        """T12 — ``none_incurred`` is distinguished from ``unknown`` by the ROW COUNT.

        A usage row exists and totals zero. ``SUM`` over zero rows also returns
        ``0``, so the sum alone cannot tell these apart — the count is the signal.
        Here ``$0.00`` is the honest rendering, the opposite claim from T11.
        """
        await seed_cost(session, "run-free", "0.000000")
        activity = FakeActivityService([lineage_row("run-free", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID)])

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        cost = response.json()["items"][0]["cost"]
        assert cost["status"] == "none_incurred", "a run with real zero-cost rows must be a MEASURED zero, not unknown"
        assert cost["amount_usd"] == money("0")

    async def test_known_cost_is_joined_on_the_event_id_not_the_keda_job_name(self, session, caller_user_row):
        """T13 — the join key is the ``event_id`` (#1756, ``assert_join_key_is_event_id``).

        The lineage row's ``run_id`` attribute is a KEDA job name and the usage
        row is keyed by ``event_id``. Joining on the plausible-sounding ``run_id``
        returns zero rows and reports the run as free, so this asserts a real
        amount came back *and* that the KEDA name never reaches the client.
        """
        activity = FakeActivityService(
            [lineage_row("evt-real-join-key", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID, run_id="agent-gateway-worker-zz999")]
        )
        await seed_cost(session, "evt-real-join-key", "12.345678", rows=3)

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        item = response.json()["items"][0]
        assert item["run_id"] == "evt-real-join-key"
        assert item["cost"]["status"] == "known"
        assert item["cost"]["amount_usd"] == money("12.345678"), "cost did not resolve — the join used the KEDA job name, not the event id"
        assert "agent-gateway-worker-zz999" not in response.text

    async def test_sub_cent_precision_survives_the_round_trip(self, session, caller_user_row):
        """T14 — most individual agent calls are sub-cent; a 2dp render loses them.

        ``usage_logs.cost_usd`` is ``NUMERIC(10,6)`` and the wire format is a
        string at that precision, so neither a float round-trip nor a currency
        rounding may eat the figure.
        """
        await seed_cost(session, "run-tiny", "0.000523")
        activity = FakeActivityService([lineage_row("run-tiny", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID)])

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        cost = response.json()["items"][0]["cost"]
        assert cost["amount_usd"] == money("0.000523")
        assert isinstance(cost["amount_usd"], str), "money must be a JSON string; a float loses NUMERIC(10,6) precision"
        assert "E" not in cost["amount_usd"].upper(), "scientific notation breaks plain decimal parsers"

    async def test_every_figure_carries_its_scope(self, session, caller_user_row):
        """T15 — a figure without its scope reads as "what this cost" (R-N5c).

        Agent-run Bedrock spend only: CodeBuild, EKS, NAT and storage are
        excluded, and someone will make a budget decision on this number.
        """
        await seed_cost(session, "run-scoped", "1.000000")
        activity = FakeActivityService([lineage_row("run-scoped", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID)])

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        body = response.json()
        expected = "agent run costs only; excludes build/infra"
        assert body["items"][0]["cost"]["scope"] == expected
        assert body["subtotal"]["scope"] == expected


# ===========================================================================
# FR-3.5 — the subtotal is a lower bound when anything is unmeasured
# ===========================================================================


class TestSubtotal:
    async def test_any_unknown_contributor_flags_the_subtotal_partial(self, session, caller_user_row):
        """T16 — a total missing an unmeasured contribution is a LOWER BOUND (FR-3.5).

        Two runs have cost, one does not. The subtotal reports what was measured
        and says so; presenting it as exact is how a decision gets made on a wrong
        number. Asserted against the measured pair's own total, so the test cannot
        pass by coincidence on a hand-written figure.
        """
        await seed_cost(session, "run-a", "10.500000")
        await seed_cost(session, "run-b", "4.250000")
        activity = FakeActivityService(
            [
                lineage_row("run-a", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID),
                lineage_row("run-b", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID),
                lineage_row("run-c-no-rows", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID),
            ]
        )

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        body = response.json()
        assert body["subtotal"]["status"] == "known"
        assert body["subtotal"]["amount_usd"] == money("14.750000")
        assert body["subtotal"]["partial"] is True, "a subtotal with an unknown contributor must be flagged partial — it is a lower bound"
        assert body["total_run_count"] == 3

    async def test_a_fully_measured_page_is_not_partial(self, session, caller_user_row):
        await seed_cost(session, "run-a", "2.000000")
        await seed_cost(session, "run-b", "3.000000")
        activity = FakeActivityService(
            [
                lineage_row("run-a", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID),
                lineage_row("run-b", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID),
            ]
        )

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        subtotal = response.json()["subtotal"]
        assert subtotal["amount_usd"] == money("5.000000")
        assert subtotal["partial"] is False

    async def test_a_page_where_nothing_is_measured_has_an_unknown_subtotal(self, session, caller_user_row):
        """T17 — no measured contribution at all means the subtotal itself is unknown.

        Summing to ``$0.00`` here is the EPIC's headline failure in miniature: a
        screen saying "these runs cost you nothing" when nothing was measured.
        """
        activity = FakeActivityService(
            [
                lineage_row("run-x", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID),
                lineage_row("run-y", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID),
            ]
        )

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        subtotal = response.json()["subtotal"]
        assert subtotal["status"] == "unknown", "an all-unknown page must not sum to a number"
        assert subtotal["amount_usd"] is None
        assert subtotal["reason"] == "no_usage_rows"
        assert subtotal["partial"] is True

    async def test_an_empty_page_is_a_measured_zero(self, session, caller_user_row):
        """T18 — no runs is a real measurement about the page, unlike an unmeasured run.

        The lineage store answered and had nothing in this window, so ``$0.00`` is
        true. Distinguished from T17 (rows existed, costs did not) and from T21
        (the store could not be asked at all).
        """
        app = build_app(session, caller_context(), FakeActivityService())
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        body = response.json()
        assert body["items"] == []
        assert body["total_run_count"] == 0
        assert body["subtotal"]["status"] == "none_incurred"
        assert body["subtotal"]["partial"] is False


# ===========================================================================
# FR-3.6 — non-calendar period types
# ===========================================================================


class TestNonCalendarPeriodTypes:
    @pytest.mark.parametrize("bad_period", ["run", "chain"])
    async def test_run_and_chain_are_422_never_500(self, session, caller_user_row, bad_period):
        """T19 — run/chain caps are lifetime-scoped (FR-3.6).

        ``get_period_start_end`` raises for them, so an unguarded call surfaces a
        ``500`` — a server error for what is a bad request. The client's mistake
        must read as one.
        """
        app = build_app(session, caller_context(), FakeActivityService())
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs", params={"period_type": bad_period})

        assert response.status_code == 422, f"period_type={bad_period} returned {response.status_code}; must be 422, never 500"

    async def test_the_second_guard_catches_a_widened_annotation(self, session, caller_user_row):
        """T20 — the guard holds even if the route's ``Literal`` is loosened.

        Two layers on purpose: FastAPI's own 422 at the boundary, and
        ``_resolve_period_bounds``' check behind it. Calling the helper directly
        is what proves the second layer exists rather than being shadowed by the
        first, so adding a period type to the annotation without teaching
        ``get_period_start_end`` about it still yields a 422.
        """
        from fastapi import HTTPException

        from src.budget.me_routes import _resolve_period_bounds

        with pytest.raises(HTTPException) as excinfo:
            _resolve_period_bounds("chain")
        assert excinfo.value.status_code == 422


# ===========================================================================
# FR-1.7 applied here — degradation vs. honest failure
# ===========================================================================


class TestDegradation:
    """The two stores fail differently, and must be reported differently."""

    async def test_cost_store_failure_returns_200_with_unknown_costs(self, session, caller_user_row):
        """T21 — a Postgres outage degrades, it does not fail the request.

        The ``activity/routes.py:176-206`` precedent: the run list is still true
        and useful without cost. The reason must be ``cost_store_unavailable``,
        not ``no_usage_rows`` — the latter asserts something about the ledger that
        was never observed.
        """
        activity = FakeActivityService([lineage_row("run-a", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID)])

        app = build_app(session, caller_context(), activity)
        with patch("src.budget.me_routes.get_cost_by_run_ids", new=AsyncMock(side_effect=OperationalError("select", {}, Exception("down")))):
            async with client_for(app) as client:
                response = await client.get("/me/budget/runs")

        assert response.status_code == 200, "a cost-store outage must not fail the run list"
        body = response.json()
        assert [item["run_id"] for item in body["items"]] == ["run-a"]
        assert body["items"][0]["cost"]["status"] == "unknown"
        assert body["items"][0]["cost"]["reason"] == "cost_store_unavailable"
        assert body["subtotal"]["status"] == "unknown"
        assert body["subtotal"]["reason"] == "cost_store_unavailable"

    async def test_lineage_store_failure_returns_503_not_an_empty_list(self, session, caller_user_row):
        """T22 — an unreadable lineage store is a 503, never a ``200 []``.

        An empty list says "no runs contributed to your spend". That is a claim,
        and it is one we cannot make when the store did not answer — the same rule
        FR-1.7 applies to spend figures.
        """

        class BrokenActivityService(FakeActivityService):
            def query_by_user(self, **kwargs):
                raise ConnectionError("dynamodb unreachable")

        app = build_app(session, caller_context(), BrokenActivityService())
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        assert response.status_code == 503
        assert "items" not in response.json(), "a lineage failure must not return a run list at all"

    async def test_a_malformed_cursor_is_a_400(self, session, caller_user_row):
        """T23 — a bad cursor is the client's error, not the server's."""

        class RejectingActivityService(FakeActivityService):
            def query_by_user(self, **kwargs):
                raise ValueError("Invalid cursor: not base64")

        app = build_app(session, caller_context(), RejectingActivityService())
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs", params={"cursor": "!!!not-base64!!!"})

        assert response.status_code == 400

    async def test_unresolvable_identity_is_reported_not_rendered_as_no_runs(self, session):
        """T24 — no ``users`` row means we could not look, not that nothing ran.

        ``resolve_canonical_user_id`` falls back to the raw Cognito sub when no
        row exists, and a lineage partition keyed by a sub matches nothing. So the
        list is empty either way — the difference is whether the response admits
        it. Note there is deliberately no ``caller_user_row`` fixture here.
        """
        app = build_app(session, caller_context(), FakeActivityService())
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        assert response.status_code == 200
        body = response.json()
        assert body["identity_status"] == "unresolved"
        assert body["subtotal"]["status"] == "unknown", "an unresolved identity must not render as a $0 subtotal"
        assert body["subtotal"]["reason"] == "lineage_unavailable"
        assert body["subtotal"]["amount_usd"] is None

    async def test_a_service_account_caller_is_not_applicable(self, session):
        """T25 — a service account has no ``users`` row by design.

        That is not a failure to resolve and must not be reported as one, or every
        service-account read would look like an outage.
        """
        app = build_app(session, caller_context(account_type="service"), FakeActivityService())
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        assert response.status_code == 200
        assert response.json()["identity_status"] == "not_applicable"

    async def test_identity_lookup_failure_returns_503(self, session, caller_user_row):
        """T26 — a Postgres failure while resolving identity is an honest 503.

        Distinct from T24: there the identity legitimately has no row, here the
        database did not answer. Reporting the second as ``unresolved`` with an
        empty list would hide an outage behind a normal-looking response.
        """
        app = build_app(session, caller_context(), FakeActivityService())
        with patch("src.budget.me_routes.resolve_canonical_user_id", new=AsyncMock(side_effect=OperationalError("select", {}, Exception("down")))):
            async with client_for(app) as client:
                response = await client.get("/me/budget/runs")

        assert response.status_code == 503


# ===========================================================================
# Pagination
# ===========================================================================


class TestPagination:
    async def test_page_size_bounds_the_page_and_is_passed_to_the_lineage_query(self, session, caller_user_row):
        """T27 — the request is bounded, so one caller cannot ask for everything.

        Also pins that ``page_size`` reaches the lineage query rather than being
        applied only after a full read.
        """
        activity = FakeActivityService(
            [lineage_row(f"run-{index:02d}", arrived_at=f"{TODAY.isoformat()}T0{index}:00:00Z", user_id=CALLER_CANONICAL_ID) for index in range(1, 6)]
        )

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs", params={"page_size": 2})

        body = response.json()
        assert len(body["items"]) == 2
        assert body["total_run_count"] == 2, "total_run_count describes THIS PAGE — see MyBudgetRunsResponse"
        assert activity.calls[0]["page_size"] == 2

    @pytest.mark.parametrize("bad_size", [0, -1, 101])
    async def test_out_of_range_page_size_is_rejected(self, session, caller_user_row, bad_size):
        app = build_app(session, caller_context(), FakeActivityService())
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs", params={"page_size": bad_size})

        assert response.status_code == 422

    async def test_the_cursor_round_trips(self, session, caller_user_row):
        """T28 — ``next_cursor`` is the lineage store's own cursor, passed through.

        A non-null cursor with few or zero items is normal (DynamoDB filters after
        the page read), so it must be surfaced rather than suppressed, and it must
        be sent back on the next request.
        """
        activity = FakeActivityService([lineage_row("run-a", arrived_at=TODAY_AT, user_id=CALLER_CANONICAL_ID)])
        activity.next_cursor = "opaque-cursor-abc"

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            first = await client.get("/me/budget/runs")
            assert first.json()["next_cursor"] == "opaque-cursor-abc"

            await client.get("/me/budget/runs", params={"cursor": "opaque-cursor-abc"})

        assert activity.calls[1]["last_key"] == "opaque-cursor-abc"

    async def test_ordering_is_newest_first(self, session, caller_user_row):
        activity = FakeActivityService(
            [
                lineage_row("run-older", arrived_at=f"{TODAY.isoformat()}T01:00:00Z", user_id=CALLER_CANONICAL_ID),
                lineage_row("run-newer", arrived_at=f"{TODAY.isoformat()}T20:00:00Z", user_id=CALLER_CANONICAL_ID),
            ]
        )

        app = build_app(session, caller_context(), activity)
        async with client_for(app) as client:
            response = await client.get("/me/budget/runs")

        assert [item["run_id"] for item in response.json()["items"]] == ["run-newer", "run-older"]


# ===========================================================================
# Contract pins — the three-valued vocabulary must not fork
# ===========================================================================


class TestContractPins:
    """``src/budget/schemas.py`` restates the cost vocabulary rather than importing it.

    That duplication is deliberate (the module is the contract of record U-5
    reads, and ``orchestration/cost.py``'s enums sit behind heavier model
    imports), but a copy that can drift is worse than an import. These tests are
    what make it a copy that cannot.
    """

    def test_cost_status_values_match_the_canonical_enum(self):
        from src.budget.schemas import CostStatusValue
        from src.orchestration.cost import CostStatus

        assert set(CostStatusValue.__args__) == {member.value for member in CostStatus}

    def test_unknown_reasons_are_a_superset_of_the_canonical_enum(self):
        """Every reason ``orchestration/cost.py`` can produce must be expressible here.

        A superset, not an equal set: this endpoint's cross-store read has two
        failure modes that module has no notion of — the cost store being
        unreadable, and the runs themselves being unenumerable. Both are pinned
        explicitly so a third cannot appear unnoticed.
        """
        from src.budget.schemas import UnknownReasonValue
        from src.orchestration.cost import UnknownReason

        ours = set(UnknownReasonValue.__args__)
        canonical = {member.value for member in UnknownReason}
        assert canonical <= ours, f"reasons the canonical module can emit are not expressible here: {sorted(canonical - ours)}"
        assert ours - canonical == {"cost_store_unavailable", "lineage_unavailable"}

    def test_cost_scope_label_matches_every_other_copy(self):
        """The label is asserted against the canonical module AND the client.

        The API and the SPA deploy independently, so a scope label that agreed
        with neither would still render — with the wrong caveat under the number.
        """
        from src.budget.schemas import COST_SCOPE_LABEL
        from src.orchestration.cost import COST_SCOPE_LABEL as CANONICAL

        assert COST_SCOPE_LABEL == CANONICAL

        frontend = Path(__file__).resolve().parents[2] / "frontend" / "src" / "utils" / "cost.ts"
        match = re.search(r"export const COST_SCOPE_LABEL = '([^']+)'", frontend.read_text())
        assert match, "COST_SCOPE_LABEL not found in frontend/src/utils/cost.ts"
        assert match.group(1) == COST_SCOPE_LABEL

    def test_unknown_cannot_carry_an_amount(self):
        """The invariant is on the model, not left to call sites.

        An ``unknown`` carrying ``0`` is the one shape that silently becomes
        ``$0.00`` three layers away, so the wire model refuses to be built that
        way at all.
        """
        from pydantic import ValidationError

        from src.budget.schemas import CostFigure

        with pytest.raises(ValidationError):
            CostFigure(status="unknown", reason="no_usage_rows", amount_usd="0")
        with pytest.raises(ValidationError):
            CostFigure(status="unknown")  # no reason
        with pytest.raises(ValidationError):
            CostFigure(status="known")  # no amount


# ===========================================================================
# Static gates — NFR-1 / NFR-2
# ===========================================================================

_SRC = Path(__file__).resolve().parents[2] / "src"


class TestStaticGates:
    def test_the_idor_prone_router_is_untouched(self):
        """T29 — this unit adds no route to ``src/budget/routes.py`` (NFR-1).

        A route there would inherit #4384's unscoped ``entity_type``/
        ``entity_id``. The issue's gate is an empty ``git diff``; asserted here as
        a route check so it holds without a git invocation in CI.
        """
        from src.budget.routes import router as legacy_router

        paths = {getattr(route, "path", "") for route in legacy_router.routes}
        assert not any("/me" in path for path in paths), f"an own-scope route was added to the IDOR-prone router: {sorted(paths)}"

    def test_no_writes_in_the_read_path(self):
        """T30 — read-only (NFR-2), matched on the AST rather than source text."""
        tree = ast.parse((_SRC / "budget" / "me_routes.py").read_text())
        called = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        called |= {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}

        forbidden = {"add", "add_all", "commit", "flush", "delete", "insert", "update", "merge", "execute_write"}
        assert not (called & forbidden), f"me_routes.py calls {sorted(called & forbidden)}; this unit is read-only (NFR-2)"

    def test_the_drill_down_never_reads_the_keda_run_id_attribute(self):
        """T31 — the cost join must not use ``InvocationItem.run_id``.

        That attribute is the KEDA job/pod name. Joining on it is a *successful*
        query returning zero rows, which every formatter downstream renders as
        ``$0.00`` — a wrong number that looks right. Asserted on the AST so the
        module's prose about the trap does not trip the gate.
        """
        tree = ast.parse((_SRC / "budget" / "me_routes.py").read_text())
        attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "item"
        }
        assert "run_id" not in attributes, "me_routes.py reads item.run_id (the KEDA job name); the cost join key is item.invocation_id"

    def test_the_route_is_mounted_without_an_api_prefix(self):
        """T32 — CloudFront strips the first ``/api`` (#4330)."""
        paths = {getattr(route, "path", "") for route in me_budget_router.routes}
        assert "/me/budget/runs" in paths
        assert not any(path.startswith("/api") for path in paths)
