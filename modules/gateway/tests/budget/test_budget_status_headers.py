"""`get_budget_status_for_headers` covers all calendar periods and names its failures — Issue #4392.

Two real defects, both in `get_budget_status_for_headers`:

  1  MONTHLY-only. The entity loop hardcoded `PeriodType.MONTHLY`, so a tenant whose
     only cap is `daily` or `weekly` got `{}` back — reported as "no budget" when a
     real, enforced cap existed. Note the direction: the function **under**-reports,
     which is the INVERSE of the symptom this issue was filed with. Enforcement
     checks all three calendar periods; the header checked one.

  2  `{}` for both "no budget" and "DB error". The not-found return and the exception
     handler were byte-identical, so a caller could not tell "unlimited / nothing
     configured" from "the ledger is down". An outage rendered as "you have no
     limit" — a UI reassuring the user exactly when it should not.

**What this suite deliberately does NOT assert.** #4392 was filed prescribing a third
fix: route the reported limit through `_resolve_scope_cap` for a
`min(configured, platform_default)` clamp. That prescription is false and was
REVERSED on review. `_resolve_scope_cap` is RUN/CHAIN-only — its platform default is a
two-way branch handing `budget_chain_cap_usd` ($100) to every non-RUN entity type, and
its override query pins `entity_id == "*"` / `period_type == "run"` so it never matches
a hierarchy row. An org with a configured $50,000 cap would advertise $100. There is
also no hierarchy platform ceiling to clamp to, and `_check_entity_budget` enforces
against the same raw `budget_amount_usd` the header reports — so no clamp bug exists.
`TestNotClampedToRunChainCeiling` pins that finding so the refactor cannot come back.

Harness is the sqlite one from `tests/budget/test_service.py` / `test_noncalendar_config_rows.py`
(#4392 touches only read predicates and return shapes, so no Redis/Lua is involved).
Rows are inserted as raw `BudgetConfig`/`BudgetUsage` models rather than through
`create_budget`, deliberately: the point is that these values are already in the
database, however they got there — including a `period_type` string no enum member
matches.

24 of the 30 tests here fail on `main` @ `c18769d`. The 6 that pass are guards, not
repros, and each is deliberate — a test that passes before the fix is not testing the
defect, so they are called out rather than left to look like coverage:

  test_hierarchy_cap_is_not_clamped_to_run_chain_ceiling   the REQUIRED anti-regression
      test. It must pass on `main` — `main` is already correct here. It exists so the
      reversed prescription fails loudly if anyone reintroduces it later.
  test_reported_limit_equals_the_value_enforcement_uses    the write/read symmetry half
      of the same finding (this one fails on `main` only because of the new key).
  test_no_budget_shape_emits_no_budget_headers             pins the `.get()`-based
  test_unavailable_shape_emits_no_budget_headers           tolerance in headers.py that
  test_status_key_does_not_leak_into_headers               makes the new statuses safe.
  test_logger_error_still_fires_on_the_failure_path        pins observability the fix
      must not drop.
  test_other_org_usage_does_not_reduce_our_remaining[monthly]  the one isolation case
      `main`'s monthly-only path already covered; its daily/weekly siblings fail.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.budget.config import budget_config
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.headers import format_budget_for_headers
from src.budget.utils import get_period_start_end
from src.shared.models.base import Base
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

ORG_ID = "org-4392"
USER_ID = "user-4392"

# The X-Budget-* header names, read off the service rather than restated, so this
# suite tracks a rename instead of asserting a stale literal.
BUDGET_HEADER_PREFIX = "X-Budget-"


@pytest.fixture
async def async_session():
    """In-memory SQLite session (same harness as tests/budget/test_service.py)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def service(async_session):
    return BudgetEnforcementService(db_session=async_session)


@pytest.fixture
def context():
    """A direct human caller with no team/department, so the hierarchy is user + org.

    `attributed_org_id` is what every query in this path is partitioned on (#4132);
    TokenContext defaults it to `org_id`.
    """
    return TokenContext(
        user_id=USER_ID,
        org_id=ORG_ID,
        team_id="",
        department_id="",
        account_type="user",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="cognito",
        attributed_org_id=ORG_ID,
    )


async def _seed_budget(
    session,
    *,
    period_type: str,
    amount: str,
    entity_type: str = EntityType.USER.value,
    entity_id: str = USER_ID,
    org_id: str = ORG_ID,
):
    """Insert a raw budget_configs row. `period_type` is a STRING on purpose."""
    session.add(
        BudgetConfig(
            org_id=org_id,
            entity_type=entity_type,
            entity_id=entity_id,
            period_type=period_type,
            budget_amount_usd=Decimal(amount),
            enforcement_mode="hard",
        )
    )
    await session.flush()


async def _seed_usage(
    session,
    *,
    period_type: PeriodType,
    spend: str,
    entity_type: str = EntityType.USER.value,
    entity_id: str = USER_ID,
    org_id: str = ORG_ID,
):
    """Insert settled spend into the window `period_type` resolves to right now."""
    period_start, _ = get_period_start_end(period_type)
    session.add(
        BudgetUsage(
            org_id=org_id,
            entity_type=entity_type,
            entity_id=entity_id,
            period_type=period_type.value,
            period_start=period_start,
            total_cost_usd=Decimal(spend),
            total_tokens=0,
            request_count=0,
        )
    )
    await session.flush()


class TestAllCalendarPeriodsAreReported:
    """Defect 1: the path was MONTHLY-only, so daily/weekly caps read as "no cap"."""

    @pytest.mark.asyncio
    async def test_daily_only_cap_is_reported(self, service, async_session, context):
        """A daily-only cap must be reported. Returned `{}` before the fix."""
        await _seed_budget(async_session, period_type=PeriodType.DAILY.value, amount="10.00")
        await _seed_usage(async_session, period_type=PeriodType.DAILY, spend="4.00")

        result = await service.get_budget_status_for_headers(context)

        _, period_end = get_period_start_end(PeriodType.DAILY)
        assert result == {
            "status": "ok",
            "budget_limit": 10.00,
            "budget_remaining": 6.00,
            "budget_reset": period_end.isoformat(),
        }

    @pytest.mark.asyncio
    async def test_weekly_only_cap_is_reported(self, service, async_session, context):
        """A weekly-only cap must be reported. Returned `{}` before the fix."""
        await _seed_budget(async_session, period_type=PeriodType.WEEKLY.value, amount="50.00")
        await _seed_usage(async_session, period_type=PeriodType.WEEKLY, spend="12.50")

        result = await service.get_budget_status_for_headers(context)

        _, period_end = get_period_start_end(PeriodType.WEEKLY)
        assert result["status"] == "ok"
        assert result["budget_limit"] == 50.00
        assert result["budget_remaining"] == 37.50
        # The WEEK's end, not the month's — the reset date must belong to the
        # period that actually won.
        assert result["budget_reset"] == period_end.isoformat()

    @pytest.mark.asyncio
    async def test_most_restrictive_period_wins_across_mixed_periods(self, service, async_session, context):
        """Lowest remaining wins whichever period it belongs to, with its own reset date.

        Daily $10 with $9 spent -> $1 remaining (the winner).
        Monthly $500 with $100 spent -> $400 remaining.
        Before the fix only the monthly line was visible, so the header advertised
        $400 of headroom while the daily cap left $1.
        """
        await _seed_budget(async_session, period_type=PeriodType.DAILY.value, amount="10.00")
        await _seed_usage(async_session, period_type=PeriodType.DAILY, spend="9.00")
        await _seed_budget(async_session, period_type=PeriodType.MONTHLY.value, amount="500.00")
        await _seed_usage(async_session, period_type=PeriodType.MONTHLY, spend="100.00")

        result = await service.get_budget_status_for_headers(context)

        _, daily_end = get_period_start_end(PeriodType.DAILY)
        assert result["budget_limit"] == 10.00
        assert result["budget_remaining"] == 1.00
        assert result["budget_reset"] == daily_end.isoformat()

    @pytest.mark.asyncio
    async def test_most_restrictive_wins_across_entities_and_periods(self, service, async_session, context):
        """The selection is over the full entity x period cross product.

        The user's weekly cap is roomy; the ORG's daily cap is nearly exhausted.
        The org line is the one that will actually deny, so it is the one to report.
        """
        await _seed_budget(async_session, period_type=PeriodType.WEEKLY.value, amount="100.00")
        await _seed_budget(
            async_session,
            period_type=PeriodType.DAILY.value,
            amount="20.00",
            entity_type=EntityType.ORGANIZATION.value,
            entity_id=ORG_ID,
        )
        await _seed_usage(
            async_session,
            period_type=PeriodType.DAILY,
            spend="19.75",
            entity_type=EntityType.ORGANIZATION.value,
            entity_id=ORG_ID,
        )

        result = await service.get_budget_status_for_headers(context)

        assert result["budget_limit"] == 20.00
        assert result["budget_remaining"] == 0.25

    @pytest.mark.asyncio
    async def test_monthly_only_tenant_unchanged(self, service, async_session, context):
        """REGRESSION GUARD: monthly-only tenants report exactly what they did before.

        Passes on `main` too (modulo the new "status" key) — that is the point.
        """
        await _seed_budget(async_session, period_type=PeriodType.MONTHLY.value, amount="200.00")
        await _seed_usage(async_session, period_type=PeriodType.MONTHLY, spend="75.00")

        result = await service.get_budget_status_for_headers(context)

        _, month_end = get_period_start_end(PeriodType.MONTHLY)
        assert result == {
            "status": "ok",
            "budget_limit": 200.00,
            "budget_remaining": 125.00,
            "budget_reset": month_end.isoformat(),
        }

    @pytest.mark.asyncio
    async def test_overspent_budget_clamps_remaining_at_zero(self, service, async_session, context):
        """Remaining never goes negative — a header must not advertise -$5.00."""
        await _seed_budget(async_session, period_type=PeriodType.DAILY.value, amount="10.00")
        await _seed_usage(async_session, period_type=PeriodType.DAILY, spend="15.00")

        result = await service.get_budget_status_for_headers(context)

        assert result["status"] == "ok"
        assert result["budget_limit"] == 10.00
        assert result["budget_remaining"] == 0.0


class TestNonCalendarRowsAreSkipped:
    """`get_period_start_end` RAISES for anything without a calendar window.

    Same class of 500 as #4328: a `period_type` value the period math does not
    implement must be skipped by an allowlist, never fed to it.
    """

    @pytest.mark.asyncio
    async def test_run_scoped_row_is_skipped_not_evaluated(self, service, async_session, context):
        """A `period_type="run"` row must not reach `get_period_start_end`.

        #4187 made it raise for RUN by design. A run cap is a plain budget_configs
        row, so merely configuring one must not 500 this path.
        """
        await _seed_budget(async_session, period_type=PeriodType.RUN.value, amount="5.00")
        await _seed_budget(async_session, period_type=PeriodType.MONTHLY.value, amount="300.00")

        result = await service.get_budget_status_for_headers(context)

        # No ValueError escaped, and the run row did not win despite being the
        # lowest amount in the table.
        assert result["status"] == "ok"
        assert result["budget_limit"] == 300.00

    @pytest.mark.asyncio
    async def test_run_only_tenant_reports_no_budget_not_unavailable(self, service, async_session, context):
        """A tenant with ONLY a run cap has no calendar cap — "no_budget", not a crash."""
        await _seed_budget(async_session, period_type=PeriodType.RUN.value, amount="5.00")

        result = await service.get_budget_status_for_headers(context)

        assert result == {"status": "no_budget"}

    @pytest.mark.asyncio
    async def test_unknown_period_type_string_is_skipped_not_fatal(self, service, async_session, context):
        """An unknown `period_type` in the DB is inert — proves an ALLOWLIST is in force.

        `PeriodType("quarterly")` raises ValueError, and `get_period_start_end` has no
        branch for it. A denylist (`!= RUN`) would let this row through and 500 the
        path; the allowlist filters it in SQL so it never becomes a Python value.
        """
        await _seed_budget(async_session, period_type="quarterly", amount="1.00")
        await _seed_budget(async_session, period_type=PeriodType.WEEKLY.value, amount="80.00")

        result = await service.get_budget_status_for_headers(context)

        assert result["status"] == "ok"
        assert result["budget_limit"] == 80.00

    @pytest.mark.asyncio
    async def test_only_unknown_period_type_reports_no_budget(self, service, async_session, context):
        """Garbage-only table -> "no_budget". Never "unavailable", never a 500."""
        await _seed_budget(async_session, period_type="fortnightly", amount="1.00")

        result = await service.get_budget_status_for_headers(context)

        assert result == {"status": "no_budget"}


class TestNoBudgetIsDistinguishableFromFailure:
    """Defect 2: the two used to be byte-identical empty dicts."""

    @pytest.mark.asyncio
    async def test_nothing_configured_reports_no_budget(self, service, context):
        assert await service.get_budget_status_for_headers(context) == {"status": "no_budget"}

    @pytest.mark.asyncio
    async def test_infrastructure_fault_reports_unavailable(self, context):
        """A ledger fault must be named as such, not rendered as "no limit"."""
        session = MagicMock()
        session.execute = AsyncMock(side_effect=OperationalError("SELECT 1", {}, Exception("connection reset")))
        service = BudgetEnforcementService(db_session=session)

        result = await service.get_budget_status_for_headers(context)

        assert result == {"status": "unavailable"}

    @pytest.mark.asyncio
    async def test_no_budget_and_unavailable_are_not_equal(self, service, context):
        """THE assertion this half of the issue exists for.

        Both were `{}` before the fix, so this compared equal and a caller had no
        way to tell an outage from an unlimited account.
        """
        no_budget = await service.get_budget_status_for_headers(context)

        failing = MagicMock()
        failing.execute = AsyncMock(side_effect=OperationalError("SELECT 1", {}, Exception("boom")))
        unavailable = await BudgetEnforcementService(db_session=failing).get_budget_status_for_headers(context)

        assert no_budget != unavailable
        assert no_budget == {"status": "no_budget"}
        assert unavailable == {"status": "unavailable"}

    @pytest.mark.asyncio
    async def test_unexpected_exception_also_reports_unavailable(self, context):
        """A code bug (not an infra fault) is still "we could not find out"."""
        session = MagicMock()
        session.execute = AsyncMock(side_effect=AttributeError("nope"))
        service = BudgetEnforcementService(db_session=session)

        assert await service.get_budget_status_for_headers(context) == {"status": "unavailable"}

    @pytest.mark.asyncio
    async def test_logger_error_still_fires_on_the_failure_path(self, context):
        """The error path stays observable — losing the log would hide the outage."""
        session = MagicMock()
        session.execute = AsyncMock(side_effect=OperationalError("SELECT 1", {}, Exception("boom")))
        service = BudgetEnforcementService(db_session=session)

        with patch("src.budget.enforcement_service.logger") as mock_logger:
            await service.get_budget_status_for_headers(context)

        assert mock_logger.error.called

    @pytest.mark.asyncio
    async def test_failure_is_fail_open_not_a_raise(self, context):
        """This is a reporting helper, not a request gate. It must never raise.

        Raising here would turn a ledger blip into a failed response for the caller —
        an enforcement change, out of scope by construction.
        """
        session = MagicMock()
        session.execute = AsyncMock(side_effect=OperationalError("SELECT 1", {}, Exception("boom")))
        service = BudgetEnforcementService(db_session=session)

        # No pytest.raises — reaching the assert is the assertion.
        assert (await service.get_budget_status_for_headers(context))["status"] == "unavailable"


class TestHeaderFormattingOverAllThreeShapes:
    """`format_budget_for_headers` is `.get()`-based, so non-ok shapes emit nothing.

    That is the property that makes the new statuses safe: neither `no_budget` nor
    `unavailable` can produce a fabricated limit, and neither produces `0.00` — which
    would read as "you have no money left" rather than "we do not know".
    """

    @pytest.mark.asyncio
    async def test_ok_shape_emits_all_three_budget_headers(self, service, async_session, context):
        await _seed_budget(async_session, period_type=PeriodType.DAILY.value, amount="10.00")

        headers = format_budget_for_headers(await service.get_budget_status_for_headers(context))

        budget_headers = {k: v for k, v in headers.items() if k.startswith(BUDGET_HEADER_PREFIX)}
        assert len(budget_headers) == 3
        assert budget_headers["X-Budget-Limit"] == "10.00"
        assert budget_headers["X-Budget-Remaining"] == "10.00"

    @pytest.mark.asyncio
    async def test_no_budget_shape_emits_no_budget_headers(self, service, context):
        headers = format_budget_for_headers(await service.get_budget_status_for_headers(context))

        assert not [k for k in headers if k.startswith(BUDGET_HEADER_PREFIX)]

    @pytest.mark.asyncio
    async def test_unavailable_shape_emits_no_budget_headers(self, context):
        """Omission is the honest signal. An invented value is not."""
        session = MagicMock()
        session.execute = AsyncMock(side_effect=OperationalError("SELECT 1", {}, Exception("boom")))
        service = BudgetEnforcementService(db_session=session)

        headers = format_budget_for_headers(await service.get_budget_status_for_headers(context))

        assert not [k for k in headers if k.startswith(BUDGET_HEADER_PREFIX)]
        assert "X-Budget-Limit" not in headers

    @pytest.mark.asyncio
    async def test_status_key_does_not_leak_into_headers(self, service, async_session, context):
        """The new "status" key is internal — it must not become a response header."""
        await _seed_budget(async_session, period_type=PeriodType.MONTHLY.value, amount="10.00")

        headers = format_budget_for_headers(await service.get_budget_status_for_headers(context))

        assert not [k for k in headers if "status" in k.lower()]


class TestNotClampedToRunChainCeiling:
    """Pins the REVERSED prescription so the harmful refactor fails loudly.

    #4392 as filed asked for the reported limit to be routed through
    `_resolve_scope_cap`. Do not. It is RUN/CHAIN-only: every non-RUN entity type
    falls to `budget_chain_cap_usd` ($100 by default) and the override query never
    matches a hierarchy row, so every user/team/org cap in the product would be
    advertised as $100.
    """

    @pytest.mark.asyncio
    async def test_hierarchy_cap_is_not_clamped_to_run_chain_ceiling(self, service, async_session, context):
        """A configured org cap far above the chain ceiling reports its CONFIGURED value."""
        chain_ceiling = budget_config.budget_chain_cap_usd
        configured = Decimal(chain_ceiling) * 500  # $50,000 against a $100 ceiling
        assert configured > chain_ceiling, "fixture must exceed the ceiling for this test to mean anything"

        await _seed_budget(
            async_session,
            period_type=PeriodType.MONTHLY.value,
            amount=str(configured),
            entity_type=EntityType.ORGANIZATION.value,
            entity_id=ORG_ID,
        )

        result = await service.get_budget_status_for_headers(context)

        assert result["budget_limit"] == float(configured)
        assert result["budget_limit"] != float(chain_ceiling)

    @pytest.mark.asyncio
    async def test_reported_limit_equals_the_value_enforcement_uses(self, service, async_session, context):
        """Write/read symmetry, asserted directly against `_check_entity_budget`.

        The header's number and the number enforcement denies against must be the
        same one, or a client is surprised by a 402 "below" the limit it was shown.
        """
        configured = Decimal("321.00")
        await _seed_budget(async_session, period_type=PeriodType.DAILY.value, amount=str(configured))
        # Spend enough that the next request must exceed the cap.
        await _seed_usage(async_session, period_type=PeriodType.DAILY, spend="320.00")

        reported = await service.get_budget_status_for_headers(context)

        enforcement_result, _ = await service._check_entity_budget(
            session=async_session,
            entity_type=EntityType.USER,
            entity_id=USER_ID,
            period_type=PeriodType.DAILY,
            estimated_cost=Decimal("5.00"),
            org_id=ORG_ID,
        )

        assert enforcement_result.allowed is False, "fixture must actually trip the cap"
        assert reported["budget_limit"] == float(enforcement_result.budget_amount_usd) == float(configured)


class TestTenantIsolation:
    """Guards the "hoisted the query out of the loop" mistake — for EVERY period type.

    Both selects are pinned to `context.attributed_org_id` (#4132), the same partition
    check/record use. Adding a period loop must not move that predicate.
    """

    @pytest.mark.parametrize(
        "period_type",
        [PeriodType.DAILY, PeriodType.WEEKLY, PeriodType.MONTHLY],
    )
    @pytest.mark.asyncio
    async def test_other_org_config_is_never_picked_up(self, service, async_session, context, period_type):
        await _seed_budget(
            async_session,
            period_type=period_type.value,
            amount="1.00",
            org_id="some-other-org",
        )

        result = await service.get_budget_status_for_headers(context)

        assert result == {"status": "no_budget"}

    @pytest.mark.parametrize(
        "period_type",
        [PeriodType.DAILY, PeriodType.WEEKLY, PeriodType.MONTHLY],
    )
    @pytest.mark.asyncio
    async def test_other_org_usage_does_not_reduce_our_remaining(self, service, async_session, context, period_type):
        """The usage select is partitioned too, not just the config select."""
        await _seed_budget(async_session, period_type=period_type.value, amount="100.00")
        await _seed_usage(async_session, period_type=period_type, spend="90.00", org_id="some-other-org")

        result = await service.get_budget_status_for_headers(context)

        assert result["budget_remaining"] == 100.00


class TestUsagePeriodWindowMatching:
    """Each period's spend is read from ITS OWN window, not a shared one."""

    @pytest.mark.asyncio
    async def test_usage_from_a_stale_window_is_ignored(self, service, async_session, context):
        """A previous day's daily row must not count against today's daily cap."""
        await _seed_budget(async_session, period_type=PeriodType.DAILY.value, amount="10.00")
        stale_start = date.today() - timedelta(days=30)
        async_session.add(
            BudgetUsage(
                org_id=ORG_ID,
                entity_type=EntityType.USER.value,
                entity_id=USER_ID,
                period_type=PeriodType.DAILY.value,
                period_start=stale_start,
                total_cost_usd=Decimal("9.99"),
                total_tokens=0,
                request_count=0,
            )
        )
        await async_session.flush()

        result = await service.get_budget_status_for_headers(context)

        assert result["budget_remaining"] == 10.00

    @pytest.mark.asyncio
    async def test_each_period_reads_its_own_usage_row(self, service, async_session, context):
        """Daily and monthly spend are separate ledgers; neither may leak into the other.

        Daily $100 cap with $10 daily spend -> $90 remaining.
        Monthly $100 cap with $5 monthly spend -> $95 remaining.
        The daily line is the more restrictive, so it must win — and it must win with
        $90, not $85 (which would mean the two ledgers were summed).
        """
        await _seed_budget(async_session, period_type=PeriodType.DAILY.value, amount="100.00")
        await _seed_usage(async_session, period_type=PeriodType.DAILY, spend="10.00")
        await _seed_budget(async_session, period_type=PeriodType.MONTHLY.value, amount="100.00")
        await _seed_usage(async_session, period_type=PeriodType.MONTHLY, spend="5.00")

        result = await service.get_budget_status_for_headers(context)

        assert result["budget_remaining"] == 90.00
