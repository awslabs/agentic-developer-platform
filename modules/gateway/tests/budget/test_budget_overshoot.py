"""
Budget overshoot regression suite — Wave 3 of #4075 (Issue #4287).

Two soundness defects let spend sail past a cap the check said it passed:

1. **Flat pre-request estimate.** Every request was pre-charged $0.05 regardless
   of model or size, so one large call against an expensive model could overshoot
   a cap that priced it at five cents.
2. **Eventually-consistent denominator.** ``budget_usage`` only materializes once
   the ``budget-usage-tracker`` Lambda processes the chat log, minutes later — so
   a burst of concurrent requests all read the same stale total and collectively
   blew through a cap each one individually passed.

Per the #4068 gate, the tests here assert the **DENIAL** — a 402 that never
reaches the downstream app — not the plumbing that produces it. Every denial test
asserts ``app_invoked is False``, which is what makes it a real gate: it cannot be
made to pass by deleting the enforcement branch.

Harness notes:

* The reservation Lua runs **for real** against ``fakeredis`` + ``lupa``. Mocking
  the script would mock the thing under test — atomicity across N keys is the
  property being asserted, and a mock cannot have it. (``lupa`` is why five
  ``tests/ratelimit/test_redis_backend.py`` cases are xfail'd as "fakeredis
  doesn't support Lua"; it does, given the extra.)
* Time is always an **injected clock**, never ``sleep``, so TTL expiry is asserted
  deterministically (same idiom as ``test_grace_window.py``).
* Config overrides use a **real** ``BudgetConfig()`` with ``object.__setattr__``
  on the single field under test, never a ``MagicMock`` — a fully-patched config
  asserts a guarantee it never exercised (the #4046 trap #4068 exists to prevent).
* Each concurrent request gets its **own** DB session stub. The one pre-existing
  budget concurrency test (``test_integration.py``) is skipped precisely because it
  shared a session across gathered coroutines.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest

from src.budget.config import BudgetConfig
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.reservations import ReservationStore, ReservationTarget
from src.budget.utils import get_period_start_end
from src.shared.schemas.auth import TokenContext

# Cheap vs. expensive, both real entries in pricing.py's table. 60x apart on
# input, which is what makes the model-awareness assertion meaningful.
HAIKU = "anthropic.claude-3-haiku-20240307-v1:0"  # $0.00025 / $0.00125 per 1k
OPUS = "anthropic.claude-3-opus-20240229-v1:0"  # $0.015  / $0.075  per 1k

RESERVATION_TTL = 120


def _context(user_id: str = "user-123", org_id: str = "org-456") -> TokenContext:
    """A caller with no team/department, so the hierarchy is just user → org.

    ``team_id``/``department_id`` are empty strings, not None — ``TokenContext``
    types them as ``str`` and ``_get_entity_hierarchy`` treats falsy as "absent".
    Keeps the ledger stub honest: exactly two entities x three periods, with the
    budget row on the user level.
    """
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _config(**overrides) -> BudgetConfig:
    """A REAL BudgetConfig with only the named fields overridden.

    Never a MagicMock: the shipped defaults are part of what these tests assert.
    """
    config = BudgetConfig()
    for name, value in overrides.items():
        object.__setattr__(config, name, value)
    return config


def _ledger_session(budget_usd: str, settled_usd: str, *, mode: str = "hard") -> MagicMock:
    """A DB session that reports one USER budget and its settled spend.

    Models the eventually-consistent ledger honestly: ``settled_usd`` is what the
    Lambda has materialized so far, which is exactly the figure the pre-#4287
    check trusted as the whole truth. Only the user level has a budget row, so
    every other (entity, period) pair reads as "no budget configured".
    """
    budget = MagicMock()
    budget.budget_amount_usd = Decimal(budget_usd)
    budget.enforcement_mode = mode

    usage = MagicMock()
    usage.total_cost_usd = Decimal(settled_usd)

    calls = {"n": 0}

    async def execute(statement):
        # Call order per (entity, period): budget config read, then usage read.
        # Only the first pair (user/daily) has a budget configured.
        calls["n"] += 1
        result = MagicMock()
        if calls["n"] == 1:
            result.scalar_one_or_none.return_value = budget
        elif calls["n"] == 2:
            result.scalar_one_or_none.return_value = usage
        else:
            result.scalar_one_or_none.return_value = None
        return result

    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute)
    return session


class _Harness:
    """Drives the pure-ASGI budget middleware and records what happened.

    Same shape as ``test_check_unavailable_response.py::_Harness``, plus a
    ``content-length`` header (the #4287 estimate reads it) and ``body_seen``,
    which proves the middleware never consumed the request body.
    """

    def __init__(self, service: BudgetEnforcementService):
        self.app_invoked = False
        self.body_seen: bytes | None = None
        self.messages: list[dict] = []
        self.middleware = BudgetEnforcementMiddleware(self._inner_app, enforcement_service=service)

    async def _inner_app(self, scope, receive, send):
        self.app_invoked = True
        scope["state"]["token_context"]._budget_provider_started = True
        # Read the body the way a real downstream handler would, so a middleware
        # that consumed it would show up here as an empty read.
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        self.body_seen = b"".join(chunks)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"message":"success"}'})

    async def post(self, path: str, *, token_context: TokenContext, body: bytes = b"{}", request_id: str = "req-1") -> None:
        scope = {
            "type": "http",
            "path": path,
            "method": "POST",
            "headers": [(b"content-length", str(len(body)).encode())],
            "state": {"token_context": token_context, "request_id": request_id},
        }

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            self.messages.append(message)

        await self.middleware(scope, receive, send)

    @property
    def status(self) -> int:
        return next(m["status"] for m in self.messages if m["type"] == "http.response.start")

    @property
    def body(self) -> dict:
        raw = b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")
        return json.loads(raw)


@pytest.fixture
def redis_client():
    """A real Lua-capable fake Redis, shared by every store in one test."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def clock():
    """Injected clock. Mutate ``clock[0]`` to advance time; never sleep."""
    return [1_000.0]


@pytest.fixture
def store(redis_client, clock):
    return ReservationStore(
        redis_url=None,
        ttl_seconds=RESERVATION_TTL,
        clock=lambda: clock[0],
        client=redis_client,
    )


def _target(headroom: str, *, org_id: str = "org-456", entity_id: str = "user-123", period: str = "daily") -> ReservationTarget:
    # `period_start` must be derived the same way production derives it, not
    # hardcoded. `reconcile_reservation` recomputes the reservation key via
    # `get_period_start_end(period_type)`, which is relative to `date.today()`.
    # A literal date here agrees with that only until the next UTC rollover,
    # after which reserve and release address different keys and the release
    # silently misses -- so the suite passed all day and broke at midnight.
    return ReservationTarget(
        org_id=org_id,
        entity_type="user",
        entity_id=entity_id,
        period_type=period,
        period_start=get_period_start_end(period)[0].isoformat(),
        headroom_usd=Decimal(headroom),
    )


# =============================================================================
# GATE — must fail on pre-fix code
# =============================================================================


class TestModelAndSizeAwareEstimate:
    """Defect 2: the pre-charge must reflect what the request actually costs."""

    @pytest.mark.asyncio
    async def test_expensive_model_is_denied_where_cheap_model_is_admitted(self, redis_client, clock):
        """GATE: same request body, different model → different verdict.

        The cap sits between the two prices, so only a model-aware estimate can
        separate them. Pre-fix both are priced at the flat $0.05 and BOTH are
        admitted — the assertion on the opus denial fails.
        """
        body = b"x" * 400_000  # ~100k tokens at 4 chars/token
        config = _config()
        verdicts = {}

        for label, model in (("haiku", HAIKU), ("opus", OPUS)):
            service = BudgetEnforcementService(
                reservations=ReservationStore(
                    redis_url=None,
                    ttl_seconds=RESERVATION_TTL,
                    clock=lambda: clock[0],
                    client=redis_client,
                )
            )
            harness = _Harness(service)
            session = _ledger_session(budget_usd="1.00", settled_usd="0.00")

            with patch.object(service, "_get_session") as get_session:
                get_session.return_value.__aenter__ = AsyncMock(return_value=session)
                get_session.return_value.__aexit__ = AsyncMock(return_value=False)
                with patch("src.budget.enforcement_service.budget_config", config):
                    await harness.post(f"/model/{model}/invoke", token_context=_context(), body=body, request_id=f"req-{label}")

            verdicts[label] = harness

        assert verdicts["haiku"].status == 200, "~$0.03 of haiku fits under a $1.00 cap and must be admitted"
        assert verdicts["haiku"].app_invoked is True

        assert verdicts["opus"].status == 402, "~$1.5k of opus must NOT be admitted against a $1.00 cap"
        assert verdicts["opus"].app_invoked is False, "the request must never reach Bedrock"
        assert verdicts["opus"].body["error"] == "budget_exceeded"

    @pytest.mark.asyncio
    async def test_large_request_is_denied_where_small_request_is_admitted(self, redis_client, clock):
        """GATE: same model, different size → different verdict.

        Pre-fix the estimate ignores size entirely, so a 100-byte request and a
        400KB one are both priced at $0.05 and both admitted.
        """
        config = _config()
        verdicts = {}

        for label, body in (("small", b"x" * 100), ("large", b"x" * 400_000)):
            service = BudgetEnforcementService(
                reservations=ReservationStore(
                    redis_url=None,
                    ttl_seconds=RESERVATION_TTL,
                    clock=lambda: clock[0],
                    client=redis_client,
                )
            )
            harness = _Harness(service)
            session = _ledger_session(budget_usd="1.00", settled_usd="0.00")

            with patch.object(service, "_get_session") as get_session:
                get_session.return_value.__aenter__ = AsyncMock(return_value=session)
                get_session.return_value.__aexit__ = AsyncMock(return_value=False)
                with patch("src.budget.enforcement_service.budget_config", config):
                    await harness.post(f"/model/{OPUS}/invoke", token_context=_context(), body=body, request_id=f"req-{label}")

            verdicts[label] = harness

        assert verdicts["small"].status == 200
        assert verdicts["large"].status == 402, "a 400KB opus request must not pass a $1.00 cap"
        assert verdicts["large"].app_invoked is False


class TestLiveDenominator:
    """Defect 3: concurrent requests must contend on a current spend figure."""

    @pytest.mark.asyncio
    async def test_single_request_over_remaining_cap_is_denied_on_stale_spend(self, redis_client, clock):
        """GATE: a request larger than the remaining headroom is not admitted.

        Cap $1.00, settled spend $0.90 → $0.10 of headroom, against a request that
        really costs ~$0.75. Pre-fix the check prices it at $0.05, sees
        $0.95 < $1.00, and admits it — overshooting the cap by 65 cents on a
        single request.
        """
        service = BudgetEnforcementService(
            reservations=ReservationStore(
                redis_url=None,
                ttl_seconds=RESERVATION_TTL,
                clock=lambda: clock[0],
                client=redis_client,
            )
        )
        harness = _Harness(service)
        session = _ledger_session(budget_usd="1.00", settled_usd="0.90")
        body = b"x" * 200_000  # ~50k opus input tokens ≈ $0.75 + output estimate

        with patch.object(service, "_get_session") as get_session:
            get_session.return_value.__aenter__ = AsyncMock(return_value=session)
            get_session.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("src.budget.enforcement_service.budget_config", _config()):
                await harness.post(f"/model/{OPUS}/invoke", token_context=_context(), body=body)

        assert harness.status == 402
        assert harness.app_invoked is False, "spend past the cap must never reach Bedrock"
        assert harness.body["error"] == "budget_exceeded"

    @pytest.mark.asyncio
    async def test_concurrent_burst_does_not_collectively_exceed_the_cap(self, redis_client, clock):
        """GATE (core defect 3): N simultaneous requests cannot sum past one cap.

        20 requests × ~$0.11 against a $1.00 cap with $0 settled. Pre-fix every
        one of them reads ``current_spend=0`` from the lagged ledger, all 20 are
        admitted, and ~$2.20 lands on a $1.00 cap. Post-fix each admission
        increments a live counter the others can see, so admissions stop at the
        cap.

        Each request gets its OWN session stub — the pre-existing (skipped)
        budget concurrency test shares one, which is why it never ran.
        """
        per_request_cost = Decimal("0.11")
        cap = Decimal("1.00")
        config = _config()

        async def one_request(index: int) -> _Harness:
            service = BudgetEnforcementService(
                reservations=ReservationStore(
                    redis_url=None,
                    ttl_seconds=RESERVATION_TTL,
                    clock=lambda: clock[0],
                    client=redis_client,
                )
            )
            harness = _Harness(service)
            session = _ledger_session(budget_usd=str(cap), settled_usd="0.00")

            with patch.object(service, "_get_session") as get_session:
                get_session.return_value.__aenter__ = AsyncMock(return_value=session)
                get_session.return_value.__aexit__ = AsyncMock(return_value=False)
                with patch("src.budget.enforcement_service.budget_config", config):
                    # Pin the estimate so the arithmetic in this test is about
                    # concurrency, not about the estimator's precision.
                    with patch.object(BudgetEnforcementMiddleware, "_estimate_cost", return_value=per_request_cost):
                        await harness.post("/v1/messages", token_context=_context(), request_id=f"req-{index}")
            return harness

        harnesses = await asyncio.gather(*(one_request(i) for i in range(20)))

        admitted = [h for h in harnesses if h.status == 200]
        denied = [h for h in harnesses if h.status == 402]

        assert len(admitted) * per_request_cost <= cap, (
            f"{len(admitted)} requests x ${per_request_cost} = "
            f"${len(admitted) * per_request_cost} admitted against a ${cap} cap — the burst overshot it"
        )
        assert denied, "once the live denominator fills, the rest must be denied"
        assert all(h.app_invoked is False for h in denied)
        assert all(h.body["error"] == "budget_exceeded" for h in denied)


# =============================================================================
# Reservation lifecycle
# =============================================================================


class TestReservationLifecycle:
    @pytest.mark.asyncio
    async def test_reservation_is_released_when_the_request_raises(self, redis_client, clock):
        """A failed request must give its headroom back.

        Asserted through the observable cap effect — a follow-up request that
        needs that headroom is ADMITTED — not by inspecting Redis keys. If a
        crashed request kept its reservation, the tenant would be throttled below
        their real spend: a self-inflicted denial of service.
        """
        service = BudgetEnforcementService(
            reservations=ReservationStore(
                redis_url=None,
                ttl_seconds=RESERVATION_TTL,
                clock=lambda: clock[0],
                client=redis_client,
            )
        )
        targets = [_target("1.00")]

        # A request reserves nearly the whole cap, then dies.
        first = await service._get_reservations().reserve("req-doomed", Decimal("0.95"), targets)
        assert first is not None and first.admitted is True

        blocked = await service._get_reservations().reserve("req-next", Decimal("0.50"), targets)
        assert blocked is not None and blocked.admitted is False, "precondition: the cap is full while the first request is in flight"

        # The proxy's `finally` fires even on exception, logging ~zero tokens.
        with patch("src.budget.enforcement_service.budget_config", _config()):
            await service.reconcile_reservation(
                context=_context(),
                request_id="req-doomed",
                model_id=OPUS,
                input_tokens=0,
                output_tokens=0,
            )

        after = await service._get_reservations().reserve("req-next", Decimal("0.50"), targets)
        assert after is not None and after.admitted is True, "a crashed request must not permanently consume cap headroom"

    @pytest.mark.asyncio
    async def test_reservation_expires_by_ttl_on_hard_kill(self, store, clock):
        """A SIGKILLed pod runs no ``finally`` — only the TTL returns the cap.

        Uses the injected clock, so the hold→release transition is asserted
        deterministically rather than by sleeping.
        """
        targets = [_target("1.00")]

        held = await store.reserve("req-killed", Decimal("0.95"), targets)
        assert held is not None and held.admitted is True

        during = await store.reserve("req-next", Decimal("0.50"), targets)
        assert during is not None and during.admitted is False, "the reservation must hold while it is live"

        clock[0] += RESERVATION_TTL + 1  # nothing ran; time simply passed

        after = await store.reserve("req-next", Decimal("0.50"), targets)
        assert after is not None and after.admitted is True, "TTL is the backstop for a reservation nobody released"

    @pytest.mark.asyncio
    async def test_reconcile_adjusts_exactly_once_for_a_repeated_request_id(self, store, clock):
        """Reconciling twice must not debit twice.

        Both directions matter: double-debiting blocks a tenant below their true
        cap (over-charge), and losing the adjustment leaves actuals unreconciled
        (under-charge). Either is a billing dispute.
        """
        targets = [_target("1.00")]

        reserved = await store.reserve("req-1", Decimal("0.90"), targets)
        assert reserved is not None and reserved.admitted is True

        # Actual came in far below the estimate.
        await store.reconcile("req-1", Decimal("0.10"), targets)
        await store.reconcile("req-1", Decimal("0.10"), targets)

        # $0.10 in flight → $0.85 must still fit. It would not if the second
        # reconcile had added another $0.10 (or re-added the estimate).
        follow_up = await store.reserve("req-2", Decimal("0.85"), targets)
        assert follow_up is not None and follow_up.admitted is True

        # ...and the reconciled $0.10 is still counted, so $0.95 must not fit.
        too_big = await store.reserve("req-3", Decimal("0.95"), targets)
        assert too_big is not None and too_big.admitted is False, "the settled actual must remain in the live denominator"

    @pytest.mark.asyncio
    async def test_reconcile_does_not_resurrect_an_expired_reservation(self, store, clock):
        """A reconcile arriving after TTL must not re-add spend.

        By then the spend is on its way to Postgres. Writing it back into the live
        counter would double-count it against the settled total once the Lambda
        lands, blocking the tenant below their true cap.
        """
        targets = [_target("1.00")]

        await store.reserve("req-slow", Decimal("0.90"), targets)
        clock[0] += RESERVATION_TTL + 1
        await store.reconcile("req-slow", Decimal("0.90"), targets)

        after = await store.reserve("req-next", Decimal("0.95"), targets)
        assert after is not None and after.admitted is True, "an expired reservation must stay gone"

    @pytest.mark.asyncio
    async def test_denied_request_leaks_no_reservation(self, store):
        """All-or-nothing across N keys: a denial must not increment any of them.

        The request is admitted against the first budget and exhausted at the
        second. A per-key increment loop would have already charged the first one,
        permanently consuming headroom for a request that never ran.
        """
        roomy = _target("10.00", period="daily")
        tight = _target("0.20", period="monthly")

        denied = await store.reserve("req-denied", Decimal("1.00"), [roomy, tight])
        assert denied is not None and denied.admitted is False
        assert denied.exhausted == tight, "the denial must name the budget that actually ran out"

        # The roomy budget must be untouched: a request needing its FULL headroom
        # is admitted, which is only true if nothing leaked into it.
        assert (await store.reserve("req-after", Decimal("10.00"), [roomy])).admitted is True

    @pytest.mark.asyncio
    async def test_reservation_is_tenant_scoped(self, store):
        """Two orgs sharing a ``user_id`` must not share a counter.

        The reservation key carries the attributed org (#4132). Omitting it would
        mean exhausting org A's cap denies org B's traffic — cross-tenant
        leakage, and a denial the second tenant can do nothing about.
        """
        org_a = _target("1.00", org_id="org-a")
        org_b = _target("1.00", org_id="org-b")

        assert (await store.reserve("req-a", Decimal("1.00"), [org_a])).admitted is True
        assert (await store.reserve("req-a2", Decimal("0.50"), [org_a])).admitted is False, "precondition: org A is exhausted"

        outcome = await store.reserve("req-b", Decimal("1.00"), [org_b])
        assert outcome is not None and outcome.admitted is True, "org A's exhaustion must not deny org B"


# =============================================================================
# Wave 1 (#4075) must keep working — Redis is a NEW hot-path dependency
# =============================================================================


class TestRedisDownDegradesToWave1:
    @pytest.mark.asyncio
    async def test_redis_down_degrades_to_settled_ledger_and_never_503s(self, clock):
        """A Redis blip with a healthy RDS must still serve traffic.

        Wave 1 kept Redis off the healthy path deliberately; Wave 3 puts it on it.
        Turning that new dependency into a 503 would be the same class of
        self-inflicted outage as the broker lockout — so an unreachable
        reservation backend degrades to the (lagged, still fail-closed)
        settled-ledger check instead.
        """
        broken = MagicMock()
        broken.register_script = MagicMock(side_effect=lambda script: AsyncMock(side_effect=ConnectionError("redis down")))

        service = BudgetEnforcementService(
            reservations=ReservationStore(
                redis_url=None,
                ttl_seconds=RESERVATION_TTL,
                clock=lambda: clock[0],
                client=broken,
            )
        )
        harness = _Harness(service)
        session = _ledger_session(budget_usd="100.00", settled_usd="0.00")

        with patch.object(service, "_get_session") as get_session:
            get_session.return_value.__aenter__ = AsyncMock(return_value=session)
            get_session.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("src.budget.enforcement_service.budget_config", _config()):
                await harness.post("/v1/messages", token_context=_context())

        assert harness.status == 200, "a Redis blip must not deny an in-budget request"
        assert harness.status != 503, "reservation unavailability is NOT a budget-check failure"
        assert harness.app_invoked is True

    @pytest.mark.asyncio
    async def test_redis_down_does_not_consume_the_db_grace_window(self, clock):
        """A reservation fault must never touch the Wave 1 grace window.

        If it did, a Redis blip would burn the DB's bounded fail-open window and
        then start denying every enforced path on the *next* real DB hiccup — a
        Redis outage escalating into a total inference outage.
        """
        broken = MagicMock()
        broken.register_script = MagicMock(side_effect=lambda script: AsyncMock(side_effect=ConnectionError("redis down")))

        grace_window = MagicMock()
        grace_window.register_failure = AsyncMock(return_value=True)
        grace_window.clear = AsyncMock()

        service = BudgetEnforcementService(
            grace_window=grace_window,
            reservations=ReservationStore(
                redis_url=None,
                ttl_seconds=RESERVATION_TTL,
                clock=lambda: clock[0],
                client=broken,
            ),
        )
        session = _ledger_session(budget_usd="100.00", settled_usd="0.00")

        with patch.object(service, "_get_session") as get_session:
            get_session.return_value.__aenter__ = AsyncMock(return_value=session)
            get_session.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("src.budget.enforcement_service.budget_config", _config()):
                result = await service.check_budget_hierarchy(_context(), Decimal("0.01"), request_id="req-1")

        assert result.allowed is True
        assert result.grace_engaged is False, "a Redis fault must not be reported as a grace-window allow"
        grace_window.register_failure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reservations_disabled_reverts_to_wave1_behaviour(self, redis_client, clock):
        """The rollback lever: reservations off → the Wave 1 check, untouched."""
        service = BudgetEnforcementService(
            reservations=ReservationStore(
                redis_url=None,
                ttl_seconds=RESERVATION_TTL,
                clock=lambda: clock[0],
                client=redis_client,
            )
        )
        harness = _Harness(service)
        session = _ledger_session(budget_usd="1.00", settled_usd="0.90")

        with patch.object(service, "_get_session") as get_session:
            get_session.return_value.__aenter__ = AsyncMock(return_value=session)
            get_session.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("src.budget.enforcement_service.budget_config", _config(budget_reservation_enabled=False)):
                # Small enough to pass the settled check; the live gate is off.
                with patch.object(BudgetEnforcementMiddleware, "_estimate_cost", return_value=Decimal("0.01")):
                    await harness.post("/v1/messages", token_context=_context())

        assert harness.status == 200
        assert harness.app_invoked is True


# =============================================================================
# Mantle passthrough guard (#2792 / #4287 D1)
# =============================================================================


class TestMantlePassthrough:
    @pytest.mark.asyncio
    async def test_middleware_does_not_consume_the_request_body(self, redis_client, clock):
        """``/openai/v1/responses`` must still forward the body byte-for-byte.

        The model-aware estimate is header-only for exactly this reason: the
        mantle route is a byte-for-byte passthrough and ``receive()`` is one-shot,
        so a body-reading estimate would starve the downstream handler. This
        asserts the handler still sees the full body.
        """
        service = BudgetEnforcementService(
            reservations=ReservationStore(
                redis_url=None,
                ttl_seconds=RESERVATION_TTL,
                clock=lambda: clock[0],
                client=redis_client,
            )
        )
        harness = _Harness(service)
        session = _ledger_session(budget_usd="100.00", settled_usd="0.00")
        body = json.dumps({"model": "gpt-5", "input": "hello"}).encode()

        with patch.object(service, "_get_session") as get_session:
            get_session.return_value.__aenter__ = AsyncMock(return_value=session)
            get_session.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("src.budget.enforcement_service.budget_config", _config()):
                await harness.post("/openai/v1/responses", token_context=_context(), body=body)

        assert harness.status == 200
        assert harness.body_seen == body, "the passthrough body must reach the handler unconsumed and unmodified"


@pytest.mark.asyncio
async def test_unknown_provider_usage_retains_ordinary_reservation(redis_client, clock):
    service = BudgetEnforcementService(
        reservations=ReservationStore(redis_url=None, ttl_seconds=RESERVATION_TTL, clock=lambda: clock[0], client=redis_client)
    )
    targets = [_target("1.00")]
    assert (await service._get_reservations().reserve("uncertain", Decimal("0.95"), targets)).admitted
    with patch("src.budget.enforcement_service.budget_config", _config()):
        await service.reconcile_reservation(
            context=_context(),
            request_id="uncertain",
            model_id=OPUS,
            input_tokens=0,
            output_tokens=0,
            actual_cost_usd=Decimal("0"),
            usage_known=False,
        )
    assert not (await service._get_reservations().reserve("next", Decimal("0.50"), targets)).admitted


@pytest.mark.asyncio
async def test_admission_and_reconcile_keep_request_start_period_across_midnight(redis_client, clock):
    service = BudgetEnforcementService(
        reservations=ReservationStore(redis_url=None, ttl_seconds=RESERVATION_TTL, clock=lambda: clock[0], client=redis_client)
    )
    context = _context()
    context._budget_request_timestamp = datetime(2026, 9, 24, 23, 59, tzinfo=UTC)
    session = _ledger_session(budget_usd="1.00", settled_usd="0.00")
    with patch.object(service, "_get_session") as get_session:
        get_session.return_value.__aenter__ = AsyncMock(return_value=session)
        get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("src.budget.enforcement_service.budget_config", _config()):
            result = await service.check_budget_hierarchy(context, Decimal("0.50"), request_id="midnight")
            assert result.allowed
            assert context._budget_admission_targets[0].period_start == "2026-09-24"
            # Actual wall clock is a later day, but reconciliation must touch
            # the same Redis field admission reserved for this request.
            with patch.object(service._get_reservations(), "reconcile", new_callable=AsyncMock) as reconcile:
                await service.reconcile_reservation(
                    context=context, request_id="midnight", model_id=OPUS, input_tokens=1, output_tokens=1, actual_cost_usd=Decimal("0.01")
                )
                assert reconcile.await_args.args[2] == context._budget_admission_targets
