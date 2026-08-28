"""Per-run and per-chain spend caps — Issue #4187.

A runaway agent could burn unbounded money inside a healthy org budget. The
hierarchy caps are monthly/daily and org-wide, so one looping run spends the whole
tenant's allowance without any single check ever failing — and nothing at all
bounds *one run*.

Per the #4068 gate, every test here asserts the **DENIAL** — a 402 that never
reaches the downstream app (``app_invoked is False``) — not the plumbing that
produces it. Deleting the enforcement branch must break these tests.

The load-bearing property, and the reason these tests are meaningful:

    Run and chain scopes have **no settled Postgres ledger**. The
    ``budget-usage-tracker`` Lambda writes no run rows, so the live Redis
    reservation total *is* the entire denominator.

So the cap trips here with no bridging job having run and no ledger row existing —
which is precisely the test an implementation built on ``SUM(cost_usd)`` fails,
because that query reads ~0 for the run currently overspending.

Harness notes (inherited from ``test_budget_overshoot.py``, the #4287 suite):

* The reservation Lua runs **for real** against ``fakeredis`` + ``lupa``. Mocking
  it would mock the thing under test — per-target TTL and all-or-nothing
  atomicity across N keys are the properties being asserted.
* Time is an **injected clock**, never ``sleep``, so TTL expiry is deterministic.
* Config overrides use a **real** ``BudgetConfig`` with ``object.__setattr__`` on
  the fields under test, never a ``MagicMock`` — a fully-patched config asserts a
  guarantee it never exercised (the #4046 trap).
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest

from src.budget.config import BudgetConfig
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.reservations import ReservationStore
from src.budget.run_binding import RunBindingResolver
from src.shared.schemas.auth import TokenContext

OPUS = "anthropic.claude-3-opus-20240229-v1:0"  # $0.015 / $0.075 per 1k

# The #4287 hierarchy TTL. Deliberately short — it is a SIGKILL backstop for one
# in-flight request. A run cap that inherited it would forget all spend older than
# two minutes, so the difference between this and the run TTL is load-bearing.
RESERVATION_TTL = 120
RUN_TTL = 86_400

RUN_ID = "evt-run-1"
CHAIN_ID = "chain-1"
CALLER = "user-123"
TENANT = "org-456"


def _context(user_id: str = CALLER, org_id: str = TENANT) -> TokenContext:
    """A caller with no team/department, so the hierarchy is just user → org."""
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

    Defaults to the feature ON and binding ENFORCING, because that is the
    configuration under test; the shipped defaults (off / shadow) get their own
    tests below.
    """
    config = BudgetConfig()
    object.__setattr__(config, "budget_run_cap_enabled", True)
    object.__setattr__(config, "budget_run_binding_mode", "enforce")
    object.__setattr__(config, "budget_run_cap_ttl_seconds", RUN_TTL)
    for name, value in overrides.items():
        object.__setattr__(config, name, value)
    return config


def _no_budget_session() -> MagicMock:
    """A ledger with NO budget rows at all.

    This is the important stub. Every hierarchy check reads "no budget
    configured", so nothing in the pre-#4187 world bounds this traffic — and there
    is no run row either, because no such row exists anywhere. Any denial in these
    tests therefore comes from the run/chain cap and nowhere else.
    """
    session = MagicMock()

    async def execute(statement):
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        return result

    session.execute = AsyncMock(side_effect=execute)
    return session


def _tenant_override_session(amount_usd: str) -> MagicMock:
    """A ledger whose ONLY row is a tenant run/chain cap override.

    The cap lookup is the first query the service makes, so returning the override
    on the first two calls (run cap, then chain cap) and nothing after leaves the
    hierarchy unbudgeted, as above.
    """
    override = MagicMock()
    override.budget_amount_usd = amount_usd

    calls = {"n": 0}

    async def execute(statement):
        calls["n"] += 1
        result = MagicMock()
        result.scalar_one_or_none.return_value = override if calls["n"] <= 2 else None
        return result

    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute)
    return session


class _StubTable:
    """The ``webhook-events`` registry, holding one row per run id."""

    def __init__(self, rows: dict[str, dict]):
        self._rows = rows

    def query(self, **kwargs):
        # KeyConditionExpression is a boto3 condition object; pull the run id out
        # of its values rather than re-implementing the DSL.
        run_id = kwargs["KeyConditionExpression"]._values[1]
        row = self._rows.get(run_id)
        return {"Items": [row] if row else []}


def _registry(**runs: str) -> _StubTable:
    """Build a registry where each run id maps to a chain id, all owned by CALLER.

    Usage: ``_registry(**{"evt-run-1": "chain-1"})``.
    """
    return _StubTable(
        {
            run_id: {
                "user_id": CALLER,
                "tenant_id": TENANT,
                "root_human_id": "",
                "correlation_id": chain_id,
                "arrived_at": "2026-08-27T10:00:00Z",
            }
            for run_id, chain_id in runs.items()
        }
    )


class _Harness:
    """Drives the pure-ASGI budget middleware and records what happened.

    Same shape as ``test_budget_overshoot.py::_Harness``, plus the
    ``x-agent-runid`` header — the asserted run id whose verification is the whole
    point of the feature.
    """

    def __init__(self, service: BudgetEnforcementService):
        self.app_invoked = False
        self.messages: list[dict] = []
        self.middleware = BudgetEnforcementMiddleware(self._inner_app, enforcement_service=service)

    async def _inner_app(self, scope, receive, send):
        self.app_invoked = True
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"message":"success"}'})

    async def post(
        self,
        path: str,
        *,
        token_context: TokenContext,
        body: bytes = b"{}",
        request_id: str = "req-1",
        run_id: str | None = RUN_ID,
    ) -> None:
        headers = [(b"content-length", str(len(body)).encode())]
        if run_id is not None:
            headers.append((b"x-agent-runid", run_id.encode()))

        scope = {
            "type": "http",
            "path": path,
            "method": "POST",
            "headers": headers,
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


def _service(redis_client, clock, table) -> BudgetEnforcementService:
    return BudgetEnforcementService(
        reservations=ReservationStore(
            redis_url=None,
            ttl_seconds=RESERVATION_TTL,
            clock=lambda: clock[0],
            client=redis_client,
        ),
        run_bindings=RunBindingResolver(
            table_name="webhook-events",
            aws_region="us-east-1",
            client=redis_client,
            table=table,
        ),
    )


async def _drive(
    service: BudgetEnforcementService,
    session: MagicMock,
    config: BudgetConfig,
    *,
    body: bytes,
    request_id: str,
    run_id: str | None = RUN_ID,
    context: TokenContext | None = None,
) -> _Harness:
    """Send one request through the middleware and return its harness."""
    harness = _Harness(service)
    with patch.object(service, "_get_session") as get_session:
        get_session.return_value.__aenter__ = AsyncMock(return_value=session)
        get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("src.budget.enforcement_service.budget_config", config):
            await harness.post(
                f"/model/{OPUS}/invoke",
                token_context=context or _context(),
                body=body,
                request_id=request_id,
                run_id=run_id,
            )
    return harness


# Roughly $0.75 of opus input, plus the pricing module's output estimate. Sized so
# a handful of these crosses a small cap while one does not.
_BIG_BODY = b"x" * 200_000


# =============================================================================
# GATE — must fail on pre-#4187 code
# =============================================================================


class TestPerRunCap:
    """A single run must not spend past its cap, whatever the org budget says."""

    @pytest.mark.asyncio
    async def test_run_is_stopped_after_its_cap_is_reached(self, redis_client, clock):
        """GATE: accumulated spend within ONE run eventually denies.

        No budget rows exist at any hierarchy level and no ledger row exists for
        the run, so pre-#4187 every one of these requests is admitted forever —
        which is the unbounded runaway the issue describes. Post-fix the run's own
        live accumulator stops it.

        The denial, not the accounting, is the assertion: the final request must
        never reach the app.
        """
        config = _config(budget_run_cap_usd=Decimal("3.00"))
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))

        statuses = []
        for i in range(4):
            harness = await _drive(
                service,
                _no_budget_session(),
                config,
                body=_BIG_BODY,
                request_id=f"req-{i}",
            )
            statuses.append(harness.status)
            last = harness

        assert 402 in statuses, f"a $3.00 run cap must stop this run; got {statuses}"
        assert last.status == 402
        assert last.app_invoked is False, "the denied request must never reach Bedrock"
        assert last.body["error"] == "budget_exceeded"
        assert last.body["details"]["scope"] == "run", "the worker keys `budget_stopped` off this discriminator"

    @pytest.mark.asyncio
    async def test_first_request_under_the_cap_is_admitted(self, redis_client, clock):
        """Regression: the cap must not deny a run that has spent nothing.

        Without this, a cap implemented as "always deny" would pass every denial
        test above.
        """
        config = _config(budget_run_cap_usd=Decimal("50.00"))
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))

        harness = await _drive(service, _no_budget_session(), config, body=_BIG_BODY, request_id="req-0")

        assert harness.status == 200
        assert harness.app_invoked is True

    @pytest.mark.asyncio
    async def test_rotating_the_run_id_header_does_not_mint_fresh_headroom(self, redis_client, clock):
        """GATE: the #3985 defect restated — a client-chosen key is not a cap.

        The caller exhausts its run cap, then sends a *different* ``X-Agent-RunId``.
        If the cap keyed on the header as given, that would be a brand-new unspent
        ledger and the run would continue indefinitely — the bypass this feature
        exists to close. The invented id has no registry row, so it is refused.
        """
        config = _config(budget_run_cap_usd=Decimal("3.00"))
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))

        for i in range(4):
            await _drive(service, _no_budget_session(), config, body=_BIG_BODY, request_id=f"req-{i}")

        rotated = await _drive(
            service,
            _no_budget_session(),
            config,
            body=_BIG_BODY,
            request_id="req-rotated",
            run_id="evt-invented-by-client",
        )

        assert rotated.status == 402, "an unverifiable run id must not buy fresh headroom"
        assert rotated.app_invoked is False

    @pytest.mark.asyncio
    async def test_a_different_run_gets_its_own_cap(self, redis_client, clock):
        """One run exhausting its cap must not deny an unrelated run.

        The counterpart to the rotation test: legitimate separate runs are
        genuinely separate ledgers, so the cap is per-run and not a shared bucket.
        """
        config = _config(budget_run_cap_usd=Decimal("3.00"), budget_chain_cap_usd=Decimal("1000.00"))
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID, "evt-run-2": "chain-2"}))

        for i in range(4):
            await _drive(service, _no_budget_session(), config, body=_BIG_BODY, request_id=f"req-{i}")

        other = await _drive(
            service,
            _no_budget_session(),
            config,
            body=_BIG_BODY,
            request_id="req-other",
            run_id="evt-run-2",
        )

        assert other.status == 200, "run 2 has spent nothing and must not inherit run 1's exhaustion"
        assert other.app_invoked is True


class TestPerChainCap:
    """A fan-out must not escape by staying inside every per-run cap."""

    @pytest.mark.asyncio
    async def test_chain_is_stopped_even_though_every_run_is_under_its_own_cap(self, redis_client, clock):
        """GATE: the hole a run-only implementation leaves.

        Three runs in one chain, each spending ~$0.79 — nowhere near the $100 run
        cap — but together over the $2.00 chain cap. A run-scoped cap alone admits
        all of them, which is the "just spawn more runs" workaround that makes a
        per-run limit cosmetic.
        """
        config = _config(budget_run_cap_usd=Decimal("100.00"), budget_chain_cap_usd=Decimal("2.00"))
        registry = _registry(**{"evt-a": CHAIN_ID, "evt-b": CHAIN_ID, "evt-c": CHAIN_ID})
        service = _service(redis_client, clock, registry)

        results = []
        for i, run in enumerate(("evt-a", "evt-b", "evt-c")):
            harness = await _drive(
                service,
                _no_budget_session(),
                config,
                body=_BIG_BODY,
                request_id=f"req-{i}",
                run_id=run,
            )
            results.append(harness)

        denied = [h for h in results if h.status == 402]
        assert denied, "a $2.00 chain cap must stop a chain of three ~$0.79 runs"
        assert denied[-1].app_invoked is False
        assert denied[-1].body["details"]["scope"] == "chain", "the chain, not the run, is what ran out"

    @pytest.mark.asyncio
    async def test_runs_in_different_chains_do_not_share_a_ledger(self, redis_client, clock):
        """Chain isolation: exhausting chain 1 must not deny chain 2."""
        config = _config(budget_run_cap_usd=Decimal("100.00"), budget_chain_cap_usd=Decimal("2.00"))
        registry = _registry(**{"evt-a": "chain-1", "evt-b": "chain-1", "evt-c": "chain-1", "evt-z": "chain-2"})
        service = _service(redis_client, clock, registry)

        for i, run in enumerate(("evt-a", "evt-b", "evt-c")):
            await _drive(service, _no_budget_session(), config, body=_BIG_BODY, request_id=f"req-{i}", run_id=run)

        other = await _drive(service, _no_budget_session(), config, body=_BIG_BODY, request_id="req-z", run_id="evt-z")

        assert other.status == 200
        assert other.app_invoked is True


class TestLifetimeAccumulator:
    """The run counter must survive far longer than the #4287 backstop TTL."""

    @pytest.mark.asyncio
    async def test_run_spend_is_remembered_past_the_hierarchy_ttl(self, redis_client, clock):
        """GATE: a run cap that forgets every 120s is not a cap.

        The #4287 reservation TTL is a SIGKILL backstop sized at ~2x p99 request
        latency. A run lasts hours. If the run accumulator inherited that TTL, a
        run pausing for three minutes would find its spend reset to zero and could
        loop forever in 120-second windows — an unbounded runaway that looks
        capped. So: exhaust the cap, advance the clock well past the hierarchy TTL
        but well inside the run TTL, and the denial must still hold.
        """
        config = _config(budget_run_cap_usd=Decimal("3.00"))
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))

        for i in range(4):
            await _drive(service, _no_budget_session(), config, body=_BIG_BODY, request_id=f"req-{i}")

        # 10 minutes later: 5x the hierarchy TTL, 1/144th of the run TTL.
        clock[0] += 600

        later = await _drive(service, _no_budget_session(), config, body=_BIG_BODY, request_id="req-later")

        assert later.status == 402, "run spend must not evaporate on the 120s hierarchy TTL"
        assert later.app_invoked is False

    @pytest.mark.asyncio
    async def test_hierarchy_reservations_keep_the_short_ttl(self, redis_client, clock):
        """Regression: #4287's backstop must be unchanged for hierarchy budgets.

        Making the TTL per-target is only safe if the default is untouched —
        otherwise a pod killed mid-request would hold a tenant's headroom hostage
        for a day instead of two minutes.
        """
        from src.budget.reservations import ReservationTarget

        store = ReservationStore(
            redis_url=None,
            ttl_seconds=RESERVATION_TTL,
            clock=lambda: clock[0],
            client=redis_client,
        )
        hierarchy = ReservationTarget(
            org_id=TENANT,
            entity_type="user",
            entity_id=CALLER,
            period_type="daily",
            period_start="2026-08-27",
            headroom_usd=Decimal("10.00"),
        )

        outcome = await store.reserve("req-a", Decimal("9.00"), [hierarchy])
        assert outcome is not None and outcome.admitted

        # Just past the backstop: the abandoned reservation must have stopped
        # consuming headroom, so a second request of the same size fits.
        clock[0] += RESERVATION_TTL + 1
        second = await store.reserve("req-b", Decimal("9.00"), [hierarchy])

        assert second is not None and second.admitted, "the 120s backstop must still release"


class TestCapResolution:
    """AD-7: the platform default is a ceiling a tenant cannot raise."""

    @pytest.mark.asyncio
    async def test_tenant_may_lower_its_own_cap(self, redis_client, clock):
        """A tighter tenant override is honoured.

        Platform default $100, tenant override $2 → the $2 figure applies, so a
        request that would fit under $100 is denied.
        """
        config = _config(budget_run_cap_usd=Decimal("100.00"), budget_chain_cap_usd=Decimal("100.00"))
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))

        statuses = []
        for i in range(3):
            harness = await _drive(service, _tenant_override_session("2.00"), config, body=_BIG_BODY, request_id=f"req-{i}")
            statuses.append(harness.status)
            last = harness

        assert 402 in statuses, f"the tenant's $2.00 override must bind, not the $100 default; got {statuses}"
        assert last.app_invoked is False

    @pytest.mark.asyncio
    async def test_tenant_cannot_raise_its_own_cap_above_the_platform_default(self, redis_client, clock):
        """GATE: the clamp. Without ``min()`` the control is self-service.

        Tenant admins can already write ``budget_configs`` rows for their own org,
        so an override of $10,000 against a $3 platform ceiling must resolve to $3.
        A missing clamp makes every cap in this file advisory.
        """
        config = _config(budget_run_cap_usd=Decimal("3.00"), budget_chain_cap_usd=Decimal("3.00"))
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))

        statuses = []
        for i in range(4):
            harness = await _drive(service, _tenant_override_session("10000.00"), config, body=_BIG_BODY, request_id=f"req-{i}")
            statuses.append(harness.status)
            last = harness

        assert 402 in statuses, f"a self-raised $10,000 cap must be clamped to the $3.00 ceiling; got {statuses}"
        assert last.app_invoked is False

    @pytest.mark.asyncio
    async def test_unparseable_override_falls_back_to_the_platform_default(self, redis_client, clock):
        """A corrupt cap row must never resolve to unlimited.

        "Absent or broken → no cap" is the failure mode the whole issue exists to
        prevent, so every error path in cap resolution lands on a finite number.
        """
        config = _config(budget_run_cap_usd=Decimal("3.00"), budget_chain_cap_usd=Decimal("3.00"))
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))

        statuses = []
        for i in range(4):
            harness = await _drive(service, _tenant_override_session("not-a-number"), config, body=_BIG_BODY, request_id=f"req-{i}")
            statuses.append(harness.status)
            last = harness

        assert 402 in statuses, f"a garbage cap row must fall back to $3.00, not to unlimited; got {statuses}"
        assert last.app_invoked is False


class TestMissingRunId:
    """ "No run id" must be a declared policy, never "unlimited"."""

    @pytest.mark.asyncio
    async def test_agent_caller_without_a_run_id_is_denied(self, redis_client, clock):
        """GATE: an IAM-authenticated caller must carry a run id.

        Agents are the callers a run cap exists to bound, and the worker always
        sends one. Letting an agent omit the header to opt out of the cap would be
        the same bypass as rotating it, with fewer steps.
        """
        config = _config()
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))
        agent_context = _context()
        object.__setattr__(agent_context, "auth_source", "iam")

        harness = await _drive(
            service,
            _no_budget_session(),
            config,
            body=b"{}",
            request_id="req-0",
            run_id=None,
            context=agent_context,
        )

        assert harness.status == 402
        assert harness.app_invoked is False

    @pytest.mark.asyncio
    async def test_human_caller_without_a_run_id_is_allowed(self, redis_client, clock):
        """A dashboard user has no run id and must not be broken by this feature.

        Their spend is bounded by the per-user hierarchy caps, which already ran —
        so nothing here is uncapped.
        """
        config = _config()
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))

        harness = await _drive(service, _no_budget_session(), config, body=b"{}", request_id="req-0", run_id=None)

        assert harness.status == 200
        assert harness.app_invoked is True


class TestRolloutSafety:
    """The feature must be inert until deliberately switched on."""

    @pytest.mark.asyncio
    async def test_disabled_by_default_changes_nothing(self, redis_client, clock):
        """The shipped default (``budget_run_cap_enabled=False``) enforces no cap.

        Uses a REAL default BudgetConfig — the point is that the value shipped in
        the file is off, not that a patched flag can be off.
        """
        config = BudgetConfig()
        assert config.budget_run_cap_enabled is False, "must ship warn-only"
        assert config.budget_run_binding_mode == "shadow", "must ship shadow-first (#3175)"

        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))

        for i in range(6):
            harness = await _drive(service, _no_budget_session(), config, body=_BIG_BODY, request_id=f"req-{i}")
            assert harness.status == 200, "no cap may fire while the feature is off"
            assert harness.app_invoked is True

    @pytest.mark.asyncio
    async def test_shadow_mode_denies_nothing_including_forged_run_ids(self, redis_client, clock):
        """Shadow mode observes drift without rejecting traffic (#3175).

        A forged run id in shadow mode is recorded and admitted. That is the whole
        value of the mode: it reports what the deny rule *would* have rejected
        before it rejects anything.
        """
        config = _config(budget_run_binding_mode="shadow", budget_run_cap_usd=Decimal("0.01"))
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))

        harness = await _drive(
            service,
            _no_budget_session(),
            config,
            body=_BIG_BODY,
            request_id="req-0",
            run_id="evt-invented",
        )

        assert harness.status == 200
        assert harness.app_invoked is True

    @pytest.mark.asyncio
    async def test_ddb_outage_degrades_to_hierarchy_caps_rather_than_denying(self, redis_client, clock):
        """A registry fault must not become a total inference outage.

        A caller cannot induce a DDB outage selectively to escape their cap, and
        the hierarchy caps still apply — so degrading costs nothing and denying
        costs everything. Same policy ``reservations.py`` applies to Redis.
        """
        from botocore.exceptions import ClientError

        class _BrokenTable:
            def query(self, **kwargs):
                raise ClientError({"Error": {"Code": "InternalServerError"}}, "Query")

        config = _config(budget_run_cap_usd=Decimal("0.01"))
        service = _service(redis_client, clock, _BrokenTable())

        harness = await _drive(service, _no_budget_session(), config, body=_BIG_BODY, request_id="req-0")

        assert harness.status == 200, "a DDB fault degrades; it must not deny"
        assert harness.app_invoked is True


class TestDenialShape:
    """A cap denial must be a 402 the client will not retry."""

    @pytest.mark.asyncio
    async def test_denial_is_402_never_503(self, redis_client, clock):
        """503 would be wrong twice over.

        The AWS SDK retries throttling-shaped responses, so a retried budget stop
        becomes a hot loop against a cap that will never clear. And 5xx says "our
        fault, try later" about a limit that is working exactly as configured —
        #4075 drew that line and this stays on the right side of it.
        """
        config = _config(budget_run_cap_usd=Decimal("3.00"))
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))

        for i in range(4):
            harness = await _drive(service, _no_budget_session(), config, body=_BIG_BODY, request_id=f"req-{i}")
            last = harness

        assert last.status == 402
        assert last.status != 503

    @pytest.mark.asyncio
    async def test_hierarchy_denial_body_is_unchanged(self, redis_client, clock):
        """Regression: a pre-#4187 denial must not grow a `scope` field.

        Existing clients parse this body. The discriminator is additive and only
        appears on the two new scopes.
        """
        config = _config(budget_run_cap_enabled=False)
        service = _service(redis_client, clock, _registry(**{RUN_ID: CHAIN_ID}))

        # One user budget of $0.01, already spent — a plain hierarchy denial.
        budget = MagicMock()
        budget.budget_amount_usd = Decimal("0.01")
        budget.enforcement_mode = "hard"
        usage = MagicMock()
        usage.total_cost_usd = Decimal("0.01")

        calls = {"n": 0}

        async def execute(statement):
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

        harness = await _drive(service, session, config, body=_BIG_BODY, request_id="req-0")

        assert harness.status == 402
        assert "scope" not in harness.body["details"], "hierarchy denials keep their pre-#4187 shape"
