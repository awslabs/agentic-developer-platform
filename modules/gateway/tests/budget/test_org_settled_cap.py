"""The org cap enforces against settled spend — Issue #4322.

The org-level cap is supposed to count **all** the money a tenant's agents spend
over a billing period. It never did. The budget-usage tracker Lambda wrote
`budget_usage` rows labelled `entity_type="organization"`, while enforcement has
always queried `EntityType.ORGANIZATION.value` == `"org"`. The two never matched,
so `_check_entity_budget` read the org's accumulated spend as `Decimal("0")` on
every request and the org line only ever tripped via the short-TTL Redis
reservation from #4287 — an in-flight window measured in seconds, not the
persisted period total. An org could run far past its intended monthly budget
with the cap never noticing.

**This suite is the reader half of the writer/reader agreement.** The writer half
lives in `tests/lambda/test_budget_usage_tracker.py::TestOrganizationEntityTypeContract`
and the historical-data half in
`tests/migrations/test_032_budget_usage_org_entity_type.py`. All three are needed
because the Lambda is a separate deploy artifact that cannot import gateway
`src`, so nothing but a test holds the two ends together — and the failure is
SILENT. Nothing raises, nothing logs; the cap simply reads an empty ledger and
every request passes. That is why the bug survived from #234 to #4322.

Per the #4068 gate, the load-bearing tests here assert the **DENIAL** — a 402
that never reaches the downstream app (`app_invoked is False`) — computed against
a non-zero settled figure. `TestStaleLabelIsInvisible` is the companion: it seeds
the ledger the way the pre-#4322 Lambda wrote it and asserts the request is
ALLOWED, which is the bug reproduced. Those two tests together are what make this
suite meaningful; either alone can be satisfied by an accident.

Harness is deliberately the one from `test_root_human_envelope.py` (#4300), which
inherited it from `test_run_spend_cap.py` (#4187): the reservation Lua runs for
real against `fakeredis` + `lupa`, and config overrides use a REAL `BudgetConfig`
via `object.__setattr__` rather than a `MagicMock` — a fully-patched config
asserts a guarantee it never exercised (the #4046 trap).

Note on scope: nothing here turns enforcement ON. The shipped defaults leave the
run-binding stack in shadow (`TestShippedDefaults` pins that), and the org line
does not depend on it — an org cap has always been read on the hierarchy path.
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
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType

OPUS = "anthropic.claude-3-opus-20240229-v1:0"  # $0.015 / $0.075 per 1k

RESERVATION_TTL = 120

TENANT = "org-456"

# The value enforcement queries `budget_usage.entity_type` with, and — since
# #4322 — the value the tracker Lambda writes. Read off the enum so this suite
# tracks it rather than restating it.
ORG = EntityType.ORGANIZATION.value

# The literal the Lambda wrote before #4322. Present ONLY so
# `TestStaleLabelIsInvisible` can seed a pre-fix ledger and prove the reader
# cannot see it. Nothing in production should write this string again.
STALE_ORG_LABEL = "organization"

# Roughly $0.75 of opus input plus the pricing module's output estimate. Sized so
# one request crosses a small cap.
_BIG_BODY = b"x" * 200_000


def _user_context(org_id: str = TENANT) -> TokenContext:
    """A plain authenticated human caller — no agent run, no binding involved.

    Deliberately the simplest path that reaches the org line: the org cap is the
    outermost tier of the hierarchy and applies to every caller, so proving it
    against a direct user keeps this suite about the ledger label and not about
    #4187's run binding.
    """
    return TokenContext(
        user_id="cognito-sub-of-a-human",
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="user",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="cognito",
    )


def _config(**overrides) -> BudgetConfig:
    """A REAL BudgetConfig with only the named fields overridden.

    The org line needs no feature flag — it predates #4187 — so the shipped
    defaults are used as-is unless a test says otherwise.
    """
    config = BudgetConfig()
    for name, value in overrides.items():
        object.__setattr__(config, name, value)
    return config


class _Ledger:
    """A stub ledger that answers by (entity_type, entity_id).

    Introspects the real SQLAlchemy statement's bound params rather than counting
    calls: the entity hierarchy grows over time (#4300 added a tier mid-sequence),
    and a call-ordinal stub would silently start answering the wrong question.

    ``budgets`` maps (entity_type, entity_id) -> cap; ``settled`` maps the same key
    -> already-spent. Anything absent reads as "no row", which for the org's
    settled line is exactly what pre-#4322 enforcement always saw.
    """

    def __init__(self, budgets: dict[tuple[str, str], str], settled: dict[tuple[str, str], str] | None = None):
        self._budgets = budgets
        self._settled = settled or {}
        self.usage_queries: list[tuple[str, str]] = []
        self.session = MagicMock()
        self.session.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, statement):
        params = statement.compile().params
        entity_type = params.get("entity_type_1")
        entity_id = params.get("entity_id_1")
        table = statement.column_descriptions[0]["entity"].__name__

        result = MagicMock()
        key = (entity_type, entity_id)

        if table == "BudgetConfig":
            cap = self._budgets.get(key)
            if cap is None:
                result.scalar_one_or_none.return_value = None
            else:
                row = MagicMock()
                row.budget_amount_usd = Decimal(cap)
                row.enforcement_mode = "hard"
                result.scalar_one_or_none.return_value = row
        else:  # BudgetUsage — the settled ledger
            # Recorded so a test can assert WHICH label the reader asked for.
            self.usage_queries.append(key)
            spend = self._settled.get(key)
            if spend is None:
                result.scalar_one_or_none.return_value = None
            else:
                row = MagicMock()
                row.total_cost_usd = Decimal(spend)
                result.scalar_one_or_none.return_value = row
        return result


class _Harness:
    """Drives the pure-ASGI budget middleware and records what happened."""

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

    async def post(self, *, token_context, body, request_id):
        scope = {
            "type": "http",
            "path": f"/model/{OPUS}/invoke",
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
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def clock():
    """Injected clock. Mutate ``clock[0]`` to advance time; never sleep."""
    return [1_000.0]


def _service(redis_client, clock) -> BudgetEnforcementService:
    return BudgetEnforcementService(
        reservations=ReservationStore(
            redis_url=None,
            ttl_seconds=RESERVATION_TTL,
            clock=lambda: clock[0],
            client=redis_client,
        )
    )


async def _drive(service, ledger: _Ledger, config: BudgetConfig, *, context: TokenContext, request_id: str, body: bytes = _BIG_BODY) -> _Harness:
    harness = _Harness(service)
    with patch.object(service, "_get_session") as get_session:
        get_session.return_value.__aenter__ = AsyncMock(return_value=ledger.session)
        get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("src.budget.enforcement_service.budget_config", config):
            await harness.post(token_context=context, body=body, request_id=request_id)
    return harness


# =============================================================================
# GATE — the org cap must enforce against the writer's rows
# =============================================================================


class TestOrgCapReadsSettledSpend:
    """The fix, stated as behaviour: settled org spend reduces the headroom."""

    @pytest.mark.asyncio
    async def test_settled_org_spend_denies_the_request(self, redis_client, clock):
        """GATE: $2 cap, $1.95 already settled under the writer's label → 402.

        The row is seeded under `EntityType.ORGANIZATION.value`, which since #4322
        is exactly what the tracker Lambda writes. Pre-#4322 that row would have
        carried `"organization"` instead, this query would have found nothing, and
        an org $1.95 into a $2 period would have been waved through.
        """
        ledger = _Ledger(budgets={(ORG, TENANT): "2.00"}, settled={(ORG, TENANT): "1.95"})

        harness = await _drive(
            _service(redis_client, clock),
            ledger,
            _config(),
            context=_user_context(),
            request_id="req-1",
        )

        assert harness.status == 402
        assert harness.app_invoked is False
        details = harness.body["details"]
        assert details["entity_type"] == ORG
        assert details["entity_id"] == TENANT
        # The settled figure the denial was computed against, not $0 — this is the
        # proof the ledger read actually landed on a row.
        assert Decimal(str(details["spent_usd"])) == Decimal("1.95")

    @pytest.mark.asyncio
    async def test_reader_queries_the_ledger_with_the_writers_label(self, redis_client, clock):
        """Pins the exact string the reader puts in the `BudgetUsage` filter.

        Asserted directly because the 402 body cannot distinguish "read the row
        and found $1.95" from "read nothing and denied on the estimate alone" when
        the cap is small enough for one request to breach it either way.
        """
        ledger = _Ledger(budgets={(ORG, TENANT): "100.00"}, settled={(ORG, TENANT): "1.00"})

        await _drive(
            _service(redis_client, clock),
            ledger,
            _config(),
            context=_user_context(),
            request_id="req-1",
            body=b"{}",
        )

        org_queries = [k for k in ledger.usage_queries if k[1] == TENANT]
        assert org_queries, "enforcement never queried the org's settled ledger"
        assert {k[0] for k in org_queries} == {ORG}
        assert STALE_ORG_LABEL not in {k[0] for k in ledger.usage_queries}

    @pytest.mark.asyncio
    async def test_settled_spend_below_the_cap_still_allows(self, redis_client, clock):
        """The converse: reading the ledger must not deny an org with headroom.

        Without this, a fix that denied unconditionally would satisfy the gate
        above — the cap would be "enforced" by being permanently closed.
        """
        ledger = _Ledger(budgets={(ORG, TENANT): "500.00"}, settled={(ORG, TENANT): "1.00"})

        harness = await _drive(
            _service(redis_client, clock),
            ledger,
            _config(),
            context=_user_context(),
            request_id="req-1",
        )

        assert harness.status == 200
        assert harness.app_invoked is True

    @pytest.mark.asyncio
    async def test_headroom_is_cap_minus_settled(self, redis_client, clock):
        """The reservation carries `cap - settled`, not the full cap.

        The in-flight window and the settled total must compose: reserving against
        the full cap would re-grant headroom the org has already spent, which is
        the #4287 half of the same accounting.
        """
        ledger = _Ledger(budgets={(ORG, TENANT): "10.00"}, settled={(ORG, TENANT): "4.00"})
        service = _service(redis_client, clock)
        captured: list = []
        original = service._reserve_or_degrade

        async def spy(request_id, estimated_cost, targets):
            captured.extend(targets)
            return await original(request_id, estimated_cost, targets)

        with patch.object(service, "_reserve_or_degrade", spy):
            await _drive(service, ledger, _config(), context=_user_context(), request_id="req-1", body=b"{}")

        org_targets = [t for t in captured if t.entity_type == ORG]
        assert org_targets, "no org reservation target was built"
        for target in org_targets:
            assert target.headroom_usd == Decimal("6.00"), "headroom must be cap - settled, not the full cap"
            assert target.entity_id == TENANT
            assert target.org_id == TENANT


class TestStaleLabelIsInvisible:
    """The bug, reproduced: a pre-#4322 ledger row cannot be seen by the reader.

    This is the test that documents WHY the migration is required rather than
    optional. The writer fix alone leaves every historical row invisible, so an
    org's accumulated period spend silently restarts from zero on deploy day —
    the "fix reader, don't migrate data" row in the issue's blast-radius table.
    """

    @pytest.mark.asyncio
    async def test_spend_recorded_under_the_old_label_does_not_count(self, redis_client, clock):
        """$1.95 settled as `"organization"` against a $2 cap → still ALLOWED.

        Asserting the permissive outcome on purpose. This is not the desired
        behaviour; it is the measured behaviour of an unmigrated database, and it
        is what makes migration 032 load-bearing rather than cosmetic.
        """
        ledger = _Ledger(budgets={(ORG, TENANT): "2.00"}, settled={(STALE_ORG_LABEL, TENANT): "1.95"})

        # The default big body: an estimate that on its own stays under the $2 cap
        # but breaches it once the $1.95 floor is added. That is what makes this an
        # A/B on the LABEL — the request is admitted here only because the settled
        # row was invisible, not because it was cheap.
        harness = await _drive(
            _service(redis_client, clock),
            ledger,
            _config(),
            context=_user_context(),
            request_id="req-1",
        )

        assert harness.status == 200
        assert harness.app_invoked is True

    @pytest.mark.asyncio
    async def test_the_same_spend_under_the_new_label_does_count(self, redis_client, clock):
        """The A/B against the test above — identical inputs, label changed.

        One character of difference in `entity_type` flips a $1.95-of-$2.00 org
        from admitted to denied. That is the entire bug, and the reason the writer
        must emit the enum value rather than a hand-written string.
        """
        ledger = _Ledger(budgets={(ORG, TENANT): "2.00"}, settled={(ORG, TENANT): "1.95"})

        harness = await _drive(
            _service(redis_client, clock),
            ledger,
            _config(),
            context=_user_context(),
            request_id="req-1",
        )

        assert harness.status == 402
        assert harness.app_invoked is False
        # Denied on the settled floor, not on the estimate alone — the sibling test
        # above admits the identical request when the floor carries the old label.
        assert Decimal(str(harness.body["details"]["spent_usd"])) == Decimal("1.95")


class TestOtherEntityLinesUnaffected:
    """Regression: the lines whose labels already agreed must be untouched.

    `user`/`team`/`agent`/`root_user` writer literals have always matched the
    reader's enum values, so #4322 must be a no-op for them. This is the issue's
    explicit regression requirement.
    """

    @pytest.mark.asyncio
    async def test_user_line_still_enforces_against_its_settled_spend(self, redis_client, clock):
        ledger = _Ledger(
            budgets={(EntityType.USER.value, "cognito-sub-of-a-human"): "2.00"},
            settled={(EntityType.USER.value, "cognito-sub-of-a-human"): "1.95"},
        )

        harness = await _drive(
            _service(redis_client, clock),
            ledger,
            _config(),
            context=_user_context(),
            request_id="req-1",
        )

        assert harness.status == 402
        assert harness.body["details"]["entity_type"] == EntityType.USER.value
        assert Decimal(str(harness.body["details"]["spent_usd"])) == Decimal("1.95")

    @pytest.mark.asyncio
    async def test_team_line_still_enforces_against_its_settled_spend(self, redis_client, clock):
        context = TokenContext(
            user_id="cognito-sub-of-a-human",
            org_id=TENANT,
            team_id="team-7",
            department_id="",
            account_type="user",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            auth_source="cognito",
        )
        ledger = _Ledger(
            budgets={(EntityType.TEAM.value, "team-7"): "2.00"},
            settled={(EntityType.TEAM.value, "team-7"): "1.95"},
        )

        harness = await _drive(_service(redis_client, clock), ledger, _config(), context=context, request_id="req-1")

        assert harness.status == 402
        assert harness.body["details"]["entity_type"] == EntityType.TEAM.value

    @pytest.mark.asyncio
    async def test_an_org_with_no_budget_row_is_allowed(self, redis_client, clock):
        """No cap configured stays permissive — #4322 must not invent enforcement.

        The issue is explicit that landing the label fix does not by itself start
        denying anyone; an org that never configured a cap must be unaffected even
        though its ledger rows are now visible.
        """
        ledger = _Ledger(budgets={}, settled={(ORG, TENANT): "9999.00"})

        harness = await _drive(
            _service(redis_client, clock),
            ledger,
            _config(),
            context=_user_context(),
            request_id="req-1",
        )

        assert harness.status == 200
        assert harness.app_invoked is True


class TestEntityTypeContract:
    """The enum value IS the cross-component contract. Guard it directly."""

    def test_organization_value_is_the_wire_literal(self):
        """`"org"`, not `"organization"` — the string both halves must agree on.

        A drift here is invisible at runtime: the usage lookup finds no row and
        every request passes. That is exactly how this bug survived from #234.
        """
        assert EntityType.ORGANIZATION.value == "org"
        assert EntityType.ORGANIZATION.value != STALE_ORG_LABEL
        # Fits budget_usage/budget_configs.entity_type String(20) — no schema
        # migration, only the data backfill in alembic 032.
        assert len(EntityType.ORGANIZATION.value) <= 20

    def test_the_ratelimit_enum_is_a_different_contract(self):
        """`src/ratelimit/models.py` keeps `ORGANIZATION = "organization"`.

        Pinned so nobody "fixes the inconsistency" by aligning the two: that enum
        keys `rate_limit_configs`, a different table with its own persisted rows,
        and changing it would break rate limiting exactly the way the budget bug
        broke caps — silently.
        """
        from src.ratelimit.models import EntityType as RateLimitEntityType

        assert RateLimitEntityType.ORGANIZATION.value == "organization"
        assert RateLimitEntityType.ORGANIZATION.value != EntityType.ORGANIZATION.value

    def test_the_tracker_lambda_agrees_with_the_reader(self):
        """The writer/reader agreement, asserted from the reader's side too.

        `tests/lambda/` owns the canonical version of this check, but the Lambda
        and the gateway are separate deploy artifacts and this suite is the one a
        gateway-side change runs. Duplicating one assertion is cheaper than an
        outage that logs nothing.

        Loaded via `importlib` because `tests.lambda` is not an importable dotted
        path — `lambda` is a reserved keyword.
        """
        import importlib.util
        from pathlib import Path

        loader_path = Path(__file__).resolve().parents[1] / "lambda" / "_handler_loader.py"
        spec = importlib.util.spec_from_file_location("_org_cap_handler_loader", loader_path)
        loader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loader)

        handler_mod = loader.load_handler("budget-usage-tracker")

        assert handler_mod._ORGANIZATION_ENTITY_TYPE == EntityType.ORGANIZATION.value


class TestShippedDefaults:
    """#4322 flips no feature flag. The spend-cap stack ships off/shadow."""

    def test_run_cap_stack_is_still_off_by_default(self):
        """The `402`-for-over-cap-orgs concern lives at the enforce flip, not here.

        Landing the label fix makes the org line read a TRUE total instead of
        zero; it does not enable the run/chain machinery. Asserted on a real
        `BudgetConfig` so a default change surfaces in this PR's suite.
        """
        config = BudgetConfig()

        assert config.budget_run_cap_enabled is False
        assert config.budget_run_binding_mode == "shadow"
