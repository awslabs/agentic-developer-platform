"""Per-root-human spend envelope — Issue #4300.

A person's budget should be the envelope for **everything they set in motion**,
not just their first hop. When a human triggers an agent and that agent fans out
into a chain of sub-agents, each sub-agent spends under its own machine identity
— so pre-#4300 none of that downstream spend counts against the human. Someone
sits comfortably under their personal cap while the chain they kicked off quietly
spends many times it.

Per the #4068 gate, the load-bearing tests here assert the **DENIAL** — a 402
that never reaches the downstream app (``app_invoked is False``) — not the
plumbing that produces it. Deleting the enforcement branch must break them.

What makes this suite meaningful, and the traps it is built to catch:

* **T1** drives N sub-agents each under a *different* service-account ``user_id``,
  each individually inside its own cap. Pre-#4300 all N+1 are admitted, because
  the only user entity checked is the agent's own service account. This is the
  test that fails today.
* **T3** pins the entity onto the HIERARCHY path, not ``_scope_targets``. The
  root-human line is CUMULATIVE per period and has a settled Postgres ledger, so
  its headroom must be ``cap - settled``, not the full cap. An implementation
  wired into ``_scope_targets`` (which the superseded D3/D7 pointed at) passes a
  naive denial test and silently ignores every dollar already settled.
* **T8** pins the id NAMESPACE. ``root_human_id`` is a canonical ``users.id``,
  while the ``user`` entity's ids are Cognito ``sub``s. A cap seeded under the
  wrong namespace finds no row, and ``_check_entity_budget`` returns
  ``allowed=True`` — feature ships, tests pass, nothing is enforced.
* **The ordering trap.** ``_get_entity_hierarchy`` must be called AFTER the run
  binding publishes ``attributed_user_id``. Built earlier, the ROOT_USER entity is
  never added and the envelope enforces nothing — while every test that seeds the
  context directly still passes. T1/T3 drive the real middleware end-to-end
  (context seeded ONLY from the registry row) specifically so that ordering is
  under test.

Harness notes (inherited from ``test_run_spend_cap.py``, the #4187 suite):

* The reservation Lua runs **for real** against ``fakeredis`` + ``lupa``.
* Config overrides use a **real** ``BudgetConfig`` with ``object.__setattr__``,
  never a ``MagicMock`` — a fully-patched config asserts a guarantee it never
  exercised (the #4046 trap).
* ``budget_run_cap_enabled`` / ``budget_run_binding_mode=enforce`` are ON here
  because the root-human value rides #4187's server-side binding: the envelope is
  inert until #4187 is flipped to enforce. That gate is asserted by
  ``TestDependencyGate``.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest

from src.budget.config import BudgetConfig
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.reservations import ReservationStore
from src.budget.run_binding import RunBindingResolver
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType

OPUS = "anthropic.claude-3-opus-20240229-v1:0"  # $0.015 / $0.075 per 1k

RESERVATION_TTL = 120
RUN_TTL = 86_400

TENANT = "org-456"
# The initiating human, in the canonical `users.id` namespace — that is how the
# lineage plane writes root_human_id (users.id FK) and how run_binding passes it
# through. NOT a Cognito sub; see T8.
HUMAN = "3f2c1b90-0000-4000-8000-00000000abcd"
# The human's Cognito sub. Same person, different namespace — a cap seeded here
# must NOT satisfy the root-human check.
HUMAN_COGNITO_SUB = "cognito-sub-for-the-human"
CHAIN_ID = "chain-1"

# The shared registry identity every hosted agent run authenticates as
# (agent-registry seed, `infra/modules/lambda-authorizer/main.tf`). It is what
# `context.user_id` holds on the hosted path — never a `users.id`. See
# TestDedupGuardReachability (Issue #4345).
HOSTED_WORKER = "scaledjob-worker"

# Issue #4344: a SERVICE-rooted root principal. This is the literal shape the
# webhook-ingress EventBridge handler writes into `root_human_id` — a rule name, not
# a person, with no `users.id` anywhere behind it.
SERVICE_KEY = "eventbridge:adp-dev-high-error-rate"
# What the ROOT_USER entity id must become for that principal.
SERVICE_ROOT_ID = f"service:{SERVICE_KEY}"

# Roughly $0.75 of opus input plus the pricing module's output estimate. Sized so
# a handful of these crosses a small cap while one does not.
_BIG_BODY = b"x" * 200_000


def _agent_context(user_id: str, org_id: str = TENANT) -> TokenContext:
    """A hosted sub-agent: its own service-account identity, no team/department.

    Deliberately does NOT set ``attributed_user_id``. The whole point is that the
    root human is resolved server-side off the registry row by the middleware, so
    seeding it here would bypass the code under test (and hide the ordering trap
    described in the module docstring).
    """
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="iam",
    )


def _config(**overrides) -> BudgetConfig:
    """A REAL BudgetConfig with only the named fields overridden.

    #4187's binding is ON and ENFORCING because #4300 rides it; the shipped
    defaults (off / shadow) get their own test in ``TestDependencyGate``.
    """
    config = BudgetConfig()
    object.__setattr__(config, "budget_run_cap_enabled", True)
    object.__setattr__(config, "budget_run_binding_mode", "enforce")
    object.__setattr__(config, "budget_run_cap_ttl_seconds", RUN_TTL)
    for name, value in overrides.items():
        object.__setattr__(config, name, value)
    return config


class _Ledger:
    """A stub ledger that answers by (entity_type, entity_id, period_type).

    Introspects the real SQLAlchemy statement's bound params rather than counting
    calls, because #4300 adds queries in the middle of the existing sequence —
    a call-ordinal stub would silently answer the wrong question once the entity
    list grows.

    ``budgets`` maps (entity_type, entity_id) -> cap. ``settled`` maps the same
    key -> already-spent. Anything absent reads as "no budget configured", which
    pre-#4300 is what the root human's line always was.
    """

    def __init__(self, budgets: dict[tuple[str, str], str], settled: dict[tuple[str, str], str] | None = None):
        self._budgets = budgets
        self._settled = settled or {}
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
            spend = self._settled.get(key)
            if spend is None:
                result.scalar_one_or_none.return_value = None
            else:
                row = MagicMock()
                row.total_cost_usd = Decimal(spend)
                result.scalar_one_or_none.return_value = row
        return result


class _StubTable:
    """The ``webhook-events`` registry, holding one row per run id."""

    def __init__(self, rows: dict[str, dict]):
        # Public so a test can mutate one row's mutable attributes (Issue #4337 added
        # `status`, the only attribute on these rows that advances over a run's life).
        self.rows = rows

    def query(self, **kwargs):
        run_id = kwargs["KeyConditionExpression"]._values[1]
        row = self.rows.get(run_id)
        return {"Items": [row] if row else []}


def _chain_registry(
    runs: dict[str, str],
    root_human_id: str = HUMAN,
    tenant: str = TENANT,
    is_human_rooted: bool | None = True,
) -> _StubTable:
    """Registry rows for a fan-out: ``{run_id: owning service-account user_id}``.

    Every row carries the SAME ``root_human_id`` — that is what makes them one
    human's chain — while each carries a DIFFERENT ``user_id``, which is what
    makes them invisible to per-user caps pre-#4300.

    ``is_human_rooted`` defaults to ``True`` because that is what every row in this
    suite models: a chain a PERSON set in motion (Issue #4344). Pass ``False`` for a
    service-rooted run, or ``None`` to omit the attribute entirely the way a row
    predating the lineage plane does — the flag is what distinguishes the two kinds
    of principal that share the ``root_human_id`` field, so it is not optional
    decoration.
    """
    return _StubTable(
        {
            run_id: {
                "user_id": owner,
                "tenant_id": tenant,
                "root_human_id": root_human_id,
                "correlation_id": CHAIN_ID,
                "arrived_at": "2026-08-28T10:00:00Z",
                **({} if is_human_rooted is None else {"is_human_rooted": is_human_rooted}),
            }
            for run_id, owner in runs.items()
        }
    )


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

    async def post(self, *, token_context, body, request_id, run_id, extra_headers=None):
        headers = [(b"content-length", str(len(body)).encode())]
        if run_id is not None:
            headers.append((b"x-agent-runid", run_id.encode()))
        headers.extend(extra_headers or [])

        scope = {
            "type": "http",
            "path": f"/model/{OPUS}/invoke",
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
    service,
    ledger: _Ledger,
    config: BudgetConfig,
    *,
    context: TokenContext,
    run_id: str,
    request_id: str,
    body: bytes = _BIG_BODY,
    extra_headers=None,
) -> _Harness:
    harness = _Harness(service)
    with patch.object(service, "_get_session") as get_session:
        get_session.return_value.__aenter__ = AsyncMock(return_value=ledger.session)
        get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("src.budget.enforcement_service.budget_config", config):
            await harness.post(
                token_context=context,
                body=body,
                request_id=request_id,
                run_id=run_id,
                extra_headers=extra_headers,
            )
    return harness


# =============================================================================
# GATE — must fail on pre-#4300 code
# =============================================================================


class TestChainEnvelope:
    """T1: the whole chain debits the initiating human's budget."""

    @pytest.mark.asyncio
    async def test_fan_out_of_sub_agents_is_stopped_by_the_humans_cap(self, redis_client, clock):
        """GATE: N sub-agents, each inside its own cap, collectively denied.

        Each sub-agent has a DIFFERENT service-account ``user_id`` and there is no
        budget row for any of them, so nothing at the per-identity level bounds
        this traffic. The only configured cap is the initiating human's.

        Pre-#4300 every request here is admitted forever: the human never appears
        in the entity hierarchy, so their cap is never read. This is the exact
        scenario the issue exists to close.
        """
        runs = {f"evt-{i}": f"svc-agent-{i}" for i in range(6)}
        ledger = _Ledger(budgets={(EntityType.ROOT_USER.value, HUMAN): "2.00"})
        service = _service(redis_client, clock, _chain_registry(runs))
        config = _config()

        statuses = []
        for i, (run_id, owner) in enumerate(runs.items()):
            harness = await _drive(
                service,
                ledger,
                config,
                context=_agent_context(owner),
                run_id=run_id,
                request_id=f"req-{i}",
            )
            statuses.append(harness.status)
            if harness.status == 402:
                # The denial must name the human's envelope, not an org/team cap:
                # an operator told to raise an org budget would change the wrong
                # knob (see stopReason.ts / agent-worker.ts).
                assert harness.body["details"]["scope"] == "root_user"
                assert harness.app_invoked is False
                break

        assert 402 in statuses, f"the human's envelope never stopped the chain: {statuses}"
        # Sanity: the cap allowed real work before it bound, so this is an
        # envelope being enforced and not a blanket deny.
        assert statuses[0] == 200

    @pytest.mark.asyncio
    async def test_each_sub_agent_alone_stays_within_its_own_cap(self, redis_client, clock):
        """The premise of T1: no per-identity cap is what stops this traffic.

        Same fan-out, but with NO root-human budget configured. Every request is
        admitted — which is today's behaviour and the hole being closed. Without
        this, T1 could pass for the wrong reason (e.g. a blanket denial).
        """
        runs = {f"evt-{i}": f"svc-agent-{i}" for i in range(6)}
        ledger = _Ledger(budgets={})
        service = _service(redis_client, clock, _chain_registry(runs))
        config = _config()

        for i, (run_id, owner) in enumerate(runs.items()):
            harness = await _drive(
                service,
                ledger,
                config,
                context=_agent_context(owner),
                run_id=run_id,
                request_id=f"req-{i}",
            )
            assert harness.status == 200, f"sub-agent {i} denied with no human cap configured"


class TestSettledFloorIsSubtracted:
    """T3: the root-human line is hierarchy-shaped, not scope-shaped."""

    @pytest.mark.asyncio
    async def test_settled_spend_counts_against_the_cap(self, redis_client, clock):
        """GATE: already-settled root-human spend reduces the headroom.

        The human's cap is $2 and $1.95 has already settled in ``budget_usage``,
        so the next real request must be denied on the remainder.

        This is the test that catches an implementation wired into
        ``_scope_targets``: that path sets ``headroom = full cap`` because run and
        chain have no settled ledger. The root-human line DOES have one (the
        tracker Lambda writes a ``root_user`` row), so ignoring it would let a
        human who has already spent their whole period's budget keep spending.
        """
        ledger = _Ledger(
            budgets={(EntityType.ROOT_USER.value, HUMAN): "2.00"},
            settled={(EntityType.ROOT_USER.value, HUMAN): "1.95"},
        )
        service = _service(redis_client, clock, _chain_registry({"evt-1": "svc-agent-1"}))

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )

        assert harness.status == 402
        assert harness.app_invoked is False
        details = harness.body["details"]
        assert details["entity_type"] == EntityType.ROOT_USER.value
        assert details["entity_id"] == HUMAN
        # The settled figure the denial was computed against, not $0 — proof the
        # ledger read actually happened.
        assert Decimal(str(details["spent_usd"])) == Decimal("1.95")

    @pytest.mark.asyncio
    async def test_reservation_headroom_is_cap_minus_settled(self, redis_client, clock):
        """The reservation target carries `cap - settled`, not the full cap.

        Asserted on the target itself because the ``_scope_targets`` mis-wiring is
        invisible in the 402 body when the settled floor happens to be zero.
        """
        ledger = _Ledger(
            budgets={(EntityType.ROOT_USER.value, HUMAN): "10.00"},
            settled={(EntityType.ROOT_USER.value, HUMAN): "4.00"},
        )
        service = _service(redis_client, clock, _chain_registry({"evt-1": "svc-agent-1"}))
        captured: list = []
        original = service._reserve_or_degrade

        async def spy(request_id, estimated_cost, targets):
            captured.extend(targets)
            return await original(request_id, estimated_cost, targets)

        with patch.object(service, "_reserve_or_degrade", spy):
            await _drive(
                service,
                ledger,
                _config(),
                context=_agent_context("svc-agent-1"),
                run_id="evt-1",
                request_id="req-1",
                body=b"{}",
            )

        root_targets = [t for t in captured if t.entity_type == EntityType.ROOT_USER.value]
        assert root_targets, "no root_user reservation target was built"
        for target in root_targets:
            assert target.headroom_usd == Decimal("6.00"), "headroom must be cap - settled, not the full cap"
            assert target.entity_id == HUMAN
            # A calendar period, never PeriodType.RUN/"lifetime" — this line is
            # cumulative per period, and get_period_start_end rejects RUN.
            assert target.period_type in ("daily", "weekly", "monthly")
            assert target.period_start != "lifetime"
            # The #4287 default TTL. A 24h TTL here would double-count: spend
            # would sit in Redis for a day AND in Postgres once the Lambda lands.
            assert target.ttl_seconds is None
            # The ledger is partitioned by attributed tenant (#4132); keying on
            # anything else breaks the Redis Cluster hash-tag invariant.
            assert target.org_id == TENANT


class TestNoDoubleCount:
    """T3 (live): one request debits each key exactly once."""

    @pytest.mark.asyncio
    async def test_root_user_and_org_keys_each_hold_the_cost_once(self, redis_client, clock):
        """The human's line is a THIRD key, not a second debit on the org's.

        Guards the aliasing bug: passing the same target twice would consume the
        human's envelope at 2x rate. Keys are disjoint by construction (entity
        type + id are both in the key), so each must hold exactly one estimate.
        """
        ledger = _Ledger(
            budgets={
                (EntityType.ROOT_USER.value, HUMAN): "100.00",
                (EntityType.ORGANIZATION.value, TENANT): "100.00",
            }
        )
        service = _service(redis_client, clock, _chain_registry({"evt-1": "svc-agent-1"}))

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )
        assert harness.status == 200

        root_keys = [k for k in await redis_client.keys("*") if f":{EntityType.ROOT_USER.value}:{HUMAN}:" in k]
        org_keys = [k for k in await redis_client.keys("*") if f":{EntityType.ORGANIZATION.value}:{TENANT}:" in k]
        assert root_keys and org_keys

        async def totals(keys):
            """Sum the reserved amounts on each key.

            Reservation hashes are ``{request_id: "amount:deadline"}``, so one
            field per in-flight request — which is itself the no-double-debit
            property: a key holding two fields for one request_id is impossible,
            and a doubled amount would show up in the sum.
            """
            out = []
            for key in keys:
                fields = await redis_client.hgetall(key)
                out.append(sum(Decimal(v.split(":")[0]) for v in fields.values()))
            return out

        root_totals = await totals(root_keys)
        org_totals = await totals(org_keys)
        # One request => one debit per key, and the human's debit equals the
        # org's rather than doubling it.
        for amount in root_totals:
            assert amount > 0
        assert sorted(root_totals) == sorted(org_totals)


# =============================================================================
# Namespace, forgery, and attribution-is-not-authz
# =============================================================================


class TestIdentityNamespace:
    """T8: the entity id is a canonical ``users.id``, not a Cognito sub."""

    @pytest.mark.asyncio
    async def test_cap_seeded_under_cognito_sub_does_not_bind(self, redis_client, clock):
        """A cap in the WRONG namespace must not silently satisfy the check.

        This is the C1 failure mode: look the human up by the wrong identifier,
        find no row, return ``allowed=True``. The feature ships, a test that seeds
        the row with the same wrong id passes, and nothing is enforced. So the
        assertion has to be that the wrong-namespace row does NOT deny.
        """
        runs = {f"evt-{i}": f"svc-agent-{i}" for i in range(6)}
        ledger = _Ledger(budgets={(EntityType.ROOT_USER.value, HUMAN_COGNITO_SUB): "0.01"})
        service = _service(redis_client, clock, _chain_registry(runs))

        for i, (run_id, owner) in enumerate(runs.items()):
            harness = await _drive(
                service,
                ledger,
                _config(),
                context=_agent_context(owner),
                run_id=run_id,
                request_id=f"req-{i}",
            )
            assert harness.status == 200, "a cap keyed on the Cognito sub must not bind the root-human line"

    @pytest.mark.asyncio
    async def test_cap_seeded_under_canonical_users_id_binds(self, redis_client, clock):
        """The same cap in the RIGHT namespace denies. Pairs with the test above."""
        ledger = _Ledger(budgets={(EntityType.ROOT_USER.value, HUMAN): "0.01"})
        service = _service(redis_client, clock, _chain_registry({"evt-1": "svc-agent-1"}))

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )
        assert harness.status == 402
        assert harness.body["details"]["entity_id"] == HUMAN


class TestRootPrincipalNamespace:
    """Issue #4344: a SERVICE root principal must never occupy the users.id namespace.

    ``root_human_id`` is written by the lineage plane for BOTH kinds of chain root: a
    canonical ``users.id`` when a person triggered it, and a service identity key
    (``eventbridge:<rule>``) when a schedule / CI job / alarm did. #4300 wrote the
    value verbatim as the ROOT_USER entity id with no test of which kind it was, so a
    service string landed in a column whose own schema comment says it holds a
    ``users.id`` — the identifier-namespace collision the EntityType comment splits
    entity values precisely to avoid, reappearing inside ``root_user``.

    The gate assertion is ``test_service_root_cannot_alias_a_human_cap``: it seeds a
    cap under the BARE service key and requires that the request is NOT denied by it.
    Pre-#4344 that cap binds, because the bare service key IS the entity id — which
    is the collision, expressed as a cap applying to the wrong principal.
    """

    def test_qualifier_table(self):
        """The one decision this fix turns on, pinned directly.

        Absent (``None``) resolving to SERVICE is the load-bearing row (D4b): rows
        predating the lineage plane, and any writer that omits the flag, must fall on
        the service side. Defaulting them to human would put the very keys this issue
        is about back into the protected namespace while every other test still
        passed.
        """
        from src.budget.enforcement_service import _qualify_root_principal_id

        # Human -> bare. Byte-identical to #4300, so already-settled ledger rows stay
        # addressable and no migration is needed.
        assert _qualify_root_principal_id(HUMAN, is_human_rooted=True) == HUMAN
        # Service -> qualified.
        assert _qualify_root_principal_id(SERVICE_KEY, is_human_rooted=False) == SERVICE_ROOT_ID
        # Absent -> service, NEVER human.
        assert _qualify_root_principal_id(SERVICE_KEY, is_human_rooted=None) == SERVICE_ROOT_ID
        # Empty stays empty: a qualified empty ("service:") would be a new sentinel
        # collapsing every unattributed request in a tenant into one bogus line.
        for flag in (True, False, None):
            assert _qualify_root_principal_id("", is_human_rooted=flag) == ""

    def test_qualified_service_id_cannot_equal_any_canonical_users_id(self):
        """The collision-freedom property, stated as an invariant rather than a case.

        A canonical ``users.id`` is a generated UUID, so it contains no colon. The
        qualified form always does. No amount of adversarial service-key naming can
        therefore produce a string a bare human id could equal — which is what makes
        one entity type safe to share.
        """
        from src.budget.enforcement_service import _qualify_root_principal_id

        assert ":" not in HUMAN, "premise: canonical users.id is a UUID and carries no colon"
        for key in (SERVICE_KEY, HUMAN, "codebuild:nightly", "a", ":", "service:already"):
            qualified = _qualify_root_principal_id(key, is_human_rooted=False)
            assert qualified.startswith("service:")
            # Even a service key that spells out a real user's id cannot alias them.
            assert qualified != HUMAN
            assert qualified != key

    @pytest.mark.asyncio
    async def test_service_root_cannot_alias_a_human_cap(self, redis_client, clock):
        """GATE (T12/T13): a cap seeded under the BARE service key must not bind.

        This is the collision made observable. Pre-#4344 the ROOT_USER entity id for
        this run IS ``eventbridge:adp-dev-high-error-rate``, so a row keyed on that
        bare string is found and the request is denied at $0.01 — i.e. a
        ``users.id``-namespace row governing a service principal. Post-fix the entity
        id is ``service:...``, the bare row is never consulted, and the request
        passes.

        Driven end-to-end through the real middleware with the context seeded ONLY
        from the registry row, so the ordering trap in the module docstring stays
        covered: the qualification has to happen where attribution is published.
        """
        registry = _chain_registry({"evt-1": "svc-agent-1"}, root_human_id=SERVICE_KEY, is_human_rooted=False)
        ledger = _Ledger(budgets={(EntityType.ROOT_USER.value, SERVICE_KEY): "0.01"})
        service = _service(redis_client, clock, registry)

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )

        assert harness.status == 200, "a cap keyed on the BARE service key must not bind — that is the namespace collision"
        assert harness.app_invoked is True
        # And nothing was reserved under the bare key either: a budget row is only
        # ever consulted, and a counter only ever created, for the qualified id.
        assert [k for k in await redis_client.keys("*") if f":{EntityType.ROOT_USER.value}:{SERVICE_KEY}:" in k] == []

    @pytest.mark.asyncio
    async def test_service_root_is_still_capped_under_its_qualified_id(self, redis_client, clock):
        """The other half: qualifying must not mean EXEMPTING (D6d).

        The cheap over-correction for this issue is to gate the entity on
        ``is_human_rooted`` and drop it for service runs — which leaves unattended CI,
        exactly the traffic that most needs a ceiling, with no root-principal cap at
        all. So the same cap seeded under the QUALIFIED id must still deny, and the 402
        must name the qualified entity because that is the id an operator has to seed a
        budget row against.

        No ``scope`` assertion: this denial comes off the settled-ledger check, which
        carries no discriminator (see ``test_org_attribution_is_unchanged``). The
        reservation path's ``scope=root_user`` is already covered by T1.
        """
        registry = _chain_registry({"evt-1": "svc-agent-1"}, root_human_id=SERVICE_KEY, is_human_rooted=False)
        ledger = _Ledger(budgets={(EntityType.ROOT_USER.value, SERVICE_ROOT_ID): "0.01"})
        service = _service(redis_client, clock, registry)

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )

        assert harness.status == 402
        assert harness.app_invoked is False
        details = harness.body["details"]
        assert details["entity_type"] == EntityType.ROOT_USER.value
        assert details["entity_id"] == SERVICE_ROOT_ID

    @pytest.mark.asyncio
    async def test_service_root_reservation_key_is_qualified(self, redis_client, clock):
        """The live Redis counter is keyed on the qualified id too.

        Asserted with a cap generous enough to admit the request, so the reservation
        actually lands rather than the check short-circuiting into a 402. Both halves
        of enforcement — the settled ledger read and the in-flight counter — must agree
        on the id, or a service principal would be metered under one key and capped
        under another.
        """
        registry = _chain_registry({"evt-1": "svc-agent-1"}, root_human_id=SERVICE_KEY, is_human_rooted=False)
        ledger = _Ledger(budgets={(EntityType.ROOT_USER.value, SERVICE_ROOT_ID): "100.00"})
        service = _service(redis_client, clock, registry)

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )

        assert harness.status == 200
        root_keys = [k for k in await redis_client.keys("*") if f":{EntityType.ROOT_USER.value}:" in k]
        assert root_keys, "a service-rooted run must still get a root-principal line (over-correction check, D6d)"
        for key in root_keys:
            assert f":{EntityType.ROOT_USER.value}:{SERVICE_ROOT_ID}:" in key, f"reservation key is not namespace-qualified: {key}"

    @pytest.mark.asyncio
    async def test_absent_flag_is_treated_as_service(self, redis_client, clock):
        """T11: a row with NO ``is_human_rooted`` resolves to service, never human.

        Rows written before the lineage plane carry no flag. Reading absence as human
        would put their ids straight back into the canonical namespace — the same bug,
        surviving on the majority of historical rows. Asserted the way the gate above
        is: the bare cap must not bind, the qualified one must.
        """
        registry = _chain_registry({"evt-1": "svc-agent-1"}, root_human_id=SERVICE_KEY, is_human_rooted=None)
        service = _service(redis_client, clock, registry)

        bare = await _drive(
            service,
            _Ledger(budgets={(EntityType.ROOT_USER.value, SERVICE_KEY): "0.01"}),
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )
        assert bare.status == 200, "an absent flag must not be read as human-rooted"

        qualified = await _drive(
            _service(redis_client, clock, registry),
            _Ledger(budgets={(EntityType.ROOT_USER.value, SERVICE_ROOT_ID): "0.01"}),
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-2",
        )
        assert qualified.status == 402
        assert qualified.body["details"]["entity_id"] == SERVICE_ROOT_ID

    @pytest.mark.asyncio
    async def test_human_root_id_stays_bare(self, redis_client, clock):
        """No regression to #4300's human path — the ids must not move.

        The settled ``root_user`` ledger rows the tracker Lambda has already written
        are keyed on the bare canonical id. If the human side gained a prefix too, the
        enforcement key would stop matching them and every human's settled floor would
        silently read as $0 — a cap that never binds, with nothing failing loudly.
        """
        registry = _chain_registry({"evt-1": "svc-agent-1"}, root_human_id=HUMAN, is_human_rooted=True)
        ledger = _Ledger(
            budgets={(EntityType.ROOT_USER.value, HUMAN): "2.00"},
            settled={(EntityType.ROOT_USER.value, HUMAN): "1.95"},
        )
        service = _service(redis_client, clock, registry)

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )

        assert harness.status == 402
        assert harness.body["details"]["entity_id"] == HUMAN, "the human's id must stay BARE"
        # Proof the settled row was still found under the bare key.
        assert Decimal(str(harness.body["details"]["spent_usd"])) == Decimal("1.95")
        for key in [k for k in await redis_client.keys("*") if f":{EntityType.ROOT_USER.value}:" in k]:
            assert "service:" not in key

    @pytest.mark.asyncio
    async def test_human_and_service_roots_never_share_an_id(self, redis_client, clock):
        """Integration: two runs whose root principals collide pre-fix stay disjoint.

        The adversarial case — a service key spelling out a real user's canonical id.
        Pre-#4344 both runs produce the identical ROOT_USER entity id, so the schedule
        spends out of that person's envelope. Post-fix the two ids share nothing.
        """
        # A schedule whose identity key is EXACTLY the human's canonical id, so the two
        # principals are indistinguishable by id alone and only the KIND separates them.
        qualified_service_id = f"service:{HUMAN}"
        # Both envelopes are configured and generous: a target is only built for an
        # entity that has a budget row, and only an admitted request reserves.
        ledger = _Ledger(
            budgets={
                (EntityType.ROOT_USER.value, HUMAN): "100.00",
                (EntityType.ROOT_USER.value, qualified_service_id): "100.00",
            }
        )
        captured: list = []

        async def run(*, human, request_id, run_id):
            registry = _chain_registry({run_id: "svc-agent-1"}, root_human_id=HUMAN, is_human_rooted=human)
            service = _service(redis_client, clock, registry)
            original = service._reserve_or_degrade

            async def spy(rid, cost, targets):
                captured.extend(t for t in targets if t.entity_type == EntityType.ROOT_USER.value)
                return await original(rid, cost, targets)

            with patch.object(service, "_reserve_or_degrade", spy):
                harness = await _drive(
                    service,
                    ledger,
                    _config(),
                    context=_agent_context("svc-agent-1"),
                    run_id=run_id,
                    request_id=request_id,
                    body=b"{}",
                )
            assert harness.status == 200

        await run(human=True, request_id="req-human", run_id="evt-1")
        await run(human=False, request_id="req-service", run_id="evt-2")

        ids = {t.entity_id for t in captured}
        assert ids == {HUMAN, qualified_service_id}, f"root principals collided: {ids}"
        # Keys, not just ids: the key is what Redis actually contends on.
        human_keys = {t.key() for t in captured if t.entity_id == HUMAN}
        service_keys = {t.key() for t in captured if t.entity_id == qualified_service_id}
        assert human_keys and service_keys
        assert human_keys.isdisjoint(service_keys), "distinct principals must not share a reservation key"

    @pytest.mark.asyncio
    async def test_service_root_is_charged_once_not_twice(self, redis_client, clock):
        """The dedup guard must survive qualification.

        For an EventBridge run the registry row names the SAME service key as both
        ``user_id`` and ``root_human_id`` (the handler passes it as both), so the
        caller IS the root principal and the SERVICE_ACCOUNT line already covers it.
        Comparing the qualified attributed id against the bare ``user_id`` would
        report "different" and add a second ROOT_USER line — one principal debited
        twice per request, its headroom consumed at 2x rate. That is the
        over-correction this asserts against.
        """
        registry = _chain_registry({"evt-1": SERVICE_KEY}, root_human_id=SERVICE_KEY, is_human_rooted=False)
        ledger = _Ledger(budgets={(EntityType.SERVICE_ACCOUNT.value, SERVICE_KEY): "100.00"})
        service = _service(redis_client, clock, registry)

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context(SERVICE_KEY),
            run_id="evt-1",
            request_id="req-1",
        )

        assert harness.status == 200
        root_keys = [k for k in await redis_client.keys("*") if f":{EntityType.ROOT_USER.value}:" in k]
        assert root_keys == [], "the caller IS the root principal — a second (root_user) line would debit it twice"


class TestForgery:
    """T4: root-human comes off the server-resolved row, never the caller."""

    @pytest.mark.asyncio
    async def test_headers_cannot_set_the_attributed_human(self, redis_client, clock):
        """Asserting a root human by header must not redirect spend.

        There is deliberately no such header. A caller who invents one must not be
        able to point their spend at somebody else's envelope (which would both
        dodge their own cap and burn an innocent person's).
        """
        victim = "victim-users-id-0000"
        ledger = _Ledger(budgets={(EntityType.ROOT_USER.value, victim): "0.01"})
        service = _service(redis_client, clock, _chain_registry({"evt-1": "svc-agent-1"}))

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
            extra_headers=[
                (b"x-agent-roothumanid", victim.encode()),
                (b"x-agent-userid", victim.encode()),
                (b"x-attributed-user-id", victim.encode()),
            ],
        )

        # The victim's $0.01 cap is never consulted, because the header is not a
        # source of attribution — the registry row is.
        assert harness.status == 200

    @pytest.mark.asyncio
    async def test_run_from_another_tenant_is_denied(self, redis_client, clock):
        """A run whose row belongs to another TENANT is a binding failure (402).

        Issue #4337 replaced the binding's caller-identity equality (which compared
        disjoint namespaces and denied all legitimate traffic) with a bearer-capability
        model whose load-bearing property is tenant scoping. So this is the case that
        now carries the forge resistance: the enforced tenant is derived from the row's
        server-written ``tenant_id``, and a caller whose attributed org disagrees is
        refused before attribution happens.

        Both forgery directions are covered — see
        ``test_run_binding.TestTenantCapabilityScope``, which also forges
        ``X-Agent-OrgId`` to the victim's org and shows the ledger still partitions on
        the row.
        """
        registry = _chain_registry(
            {"evt-1": "some-other-service-account"},
            root_human_id="a-different-human",
            tenant="org-somebody-else",
        )
        ledger = _Ledger(budgets={})
        service = _service(redis_client, clock, registry)

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )

        assert harness.status == 402
        assert harness.app_invoked is False

    @pytest.mark.asyncio
    async def test_naming_a_same_tenant_humans_run_charges_that_human_not_the_caller(self, redis_client, clock):
        """The #4337 reduction, on the record — and why it is not a spend dodge.

        Under the capability model a worker CAN bind a live run of another human in
        its own tenant (the reduction is from "the caller owns this run" to "the
        caller holds a live, tenant-consistent capability for it"). What that does
        NOT buy is escape from a cap: attribution follows the ROW, so naming
        somebody else's run charges *their* envelope, and here that envelope is
        capped at $0.01 and denies.

        So the reachable outcomes are "charged to the row's human" or "denied by the
        row's human's cap". There is no id an agent can assert that yields uncapped
        headroom, which is the property the cap actually needs.
        """
        other_human = "another-human-in-my-tenant"
        registry = _chain_registry({"evt-1": "some-other-service-account"}, root_human_id=other_human)
        ledger = _Ledger(budgets={(EntityType.ROOT_USER.value, other_human): "0.01"})
        service = _service(redis_client, clock, registry)

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )

        assert harness.status == 402
        assert harness.app_invoked is False

    @pytest.mark.asyncio
    async def test_a_finished_run_mints_no_further_headroom(self, redis_client, clock):
        """Issue #4337 property 3, end-to-end through the middleware.

        The residual bypass the removed equality was nominally covering: rotate across
        ids from one's OWN completed runs. Those ids pass every other property — real,
        unguessable, same tenant — so without the liveness check the rotation is
        unbounded in the one direction an agent genuinely controls.
        """
        registry = _chain_registry({"evt-1": "svc-agent-1"})
        registry.rows["evt-1"]["status"] = "complete"
        ledger = _Ledger(budgets={})
        service = _service(redis_client, clock, registry)

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )

        assert harness.status == 402
        assert harness.app_invoked is False


class TestAttributionIsNotAuthorization:
    """T9: the attributed human must never widen what the caller may do."""

    def test_attributed_user_id_is_not_an_authz_input(self):
        """``attributed_user_id`` does not touch identity or admin state.

        If any authz path read this field, a sub-agent could act as the human who
        triggered it. Pinned as a unit assertion on the carrier itself.
        """
        context = _agent_context("svc-agent-1")
        object.__setattr__(context, "attributed_user_id", HUMAN)

        assert context.user_id == "svc-agent-1"
        assert context.is_admin is False
        assert context.org_id == TENANT

    def test_no_authz_module_reads_the_attribution_field(self):
        """Grep-style guard: only budget/attribution code may read the field.

        Cheap, and it fails loudly the first time somebody wires attribution into
        an access-control decision — the failure mode nobody notices in review.
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[2] / "src"
        offenders = [
            path.relative_to(src).as_posix()
            for path in src.rglob("*.py")
            if "attributed_user_id" in path.read_text()
            and not path.as_posix().split("/src/")[-1].startswith(("budget/", "proxy/", "chat_logging/", "shared/schemas/"))
        ]
        assert offenders == [], f"attribution field read outside budget/attribution paths: {offenders}"


# =============================================================================
# Regression / no-op cases
# =============================================================================


class TestNoRootHuman:
    """T10: absent root-human is normal, and must change nothing."""

    @pytest.mark.asyncio
    async def test_empty_root_human_adds_no_entity(self, redis_client, clock):
        """A non-human-rooted run behaves exactly as pre-#4300.

        ``run_binding`` normalizes a missing ``root_human_id`` to ``""``, which is
        the common case (every row written before the lineage plane shipped). The
        envelope must not fire, and — critically — no reservation may be keyed on
        the empty string, which would collapse every non-human-rooted request in
        the tenant into one shared bogus counter.
        """
        registry = _chain_registry({"evt-1": "svc-agent-1"}, root_human_id="")
        ledger = _Ledger(budgets={(EntityType.ROOT_USER.value, ""): "0.01"})
        service = _service(redis_client, clock, registry)

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )

        assert harness.status == 200
        keys = await redis_client.keys("*")
        assert not [k for k in keys if f":{EntityType.ROOT_USER.value}:" in k], "no root_user counter may exist without a root human"

    @pytest.mark.asyncio
    async def test_direct_human_is_charged_exactly_once(self, redis_client, clock):
        """T11: when the caller IS the initiating human, one line, not two.

        The USER line already covers them. A second entity would reserve the same
        cost twice against one person (different entity types are different Redis
        keys, so nothing dedupes them) and consume their headroom at 2x rate.
        """
        registry = _chain_registry({"evt-1": HUMAN}, root_human_id=HUMAN)
        ledger = _Ledger(budgets={(EntityType.USER.value, HUMAN): "100.00"})
        service = _service(redis_client, clock, registry)

        human_context = TokenContext(
            user_id=HUMAN,
            org_id=TENANT,
            team_id="",
            department_id="",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        harness = await _drive(service, ledger, _config(), context=human_context, run_id="evt-1", request_id="req-1")
        assert harness.status == 200

        keys = await redis_client.keys("*")
        assert not [k for k in keys if f":{EntityType.ROOT_USER.value}:" in k], "the direct-human case must not add a second (root_user) line"

    @pytest.mark.asyncio
    async def test_org_attribution_is_unchanged(self, redis_client, clock):
        """T12: #4132's org line still behaves exactly as before."""
        ledger = _Ledger(budgets={(EntityType.ORGANIZATION.value, TENANT): "0.01"})
        service = _service(redis_client, clock, _chain_registry({"evt-1": "svc-agent-1"}))

        harness = await _drive(
            service,
            ledger,
            _config(),
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )

        assert harness.status == 402
        assert harness.body["details"]["entity_type"] == EntityType.ORGANIZATION.value
        # An org denial carries no scope discriminator, keeping every pre-#4187
        # 402 body byte-identical.
        assert "scope" not in harness.body["details"]


class TestDedupGuardReachability:
    """Issue #4345: which callers the `!= user_id` dedup skip can actually fire for.

    The guard at ``_get_entity_hierarchy`` skips the ROOT_USER entity when the caller
    IS the attributed root principal. For a HOSTED agent run those two ids can never
    be equal — ``user_id`` is the shared registry identity ``scaledjob-worker`` while
    ``attributed_user_id`` is a canonical ``users.id`` UUID — so on the path #4300 was
    written for the skip is a permanent no-op and the envelope line is always added.

    That makes the guard easy to mis-read as protecting the hosted path, and easy to
    delete as dead code. It is neither: two direct-caller shapes DO reach the equal
    case (human calling the gateway directly; the service-rooted EventBridge run whose
    row names one key as both caller and root). These tests pin both sides so the
    guard is re-validated rather than dropped, and so the day a hosted run's caller
    can coincide with its attributed user, the equal-ids assertion is already here.

    Asserted directly against ``_get_entity_hierarchy`` rather than through the
    middleware: the claim is about which entities the hierarchy builder emits for a
    given pair of ids, and seeding the context is the only way to construct the equal
    pair the live registry cannot currently produce.
    """

    @staticmethod
    def _context(user_id: str, attributed_user_id: str, account_type: str = "service") -> TokenContext:
        return TokenContext(
            user_id=user_id,
            org_id=TENANT,
            team_id="",
            department_id="",
            account_type=account_type,
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            attributed_user_id=attributed_user_id,
        )

    def test_equal_ids_add_no_root_user_entity(self):
        """Guard FIRES when the caller is the attributed root: one line, not two.

        The load-bearing assertion. Removing the ``!= user_id`` condition must break
        this test — otherwise the guard could be deleted as dead code and a
        human-caller run would silently debit the same person twice per request.
        """
        service = BudgetEnforcementService()

        entities = service._get_entity_hierarchy(self._context(HUMAN, HUMAN, account_type="human"))

        assert (EntityType.USER, HUMAN) in entities
        assert not [e for e in entities if e[0] == EntityType.ROOT_USER], "caller IS the root principal — a ROOT_USER line would debit them twice"

    def test_distinct_ids_add_the_root_user_entity(self):
        """The hosted-run reality: guard is inert, the envelope line IS created.

        This is the shape every hosted agent request has. If the guard ever started
        firing here, #4300's envelope would silently stop being enforced while every
        equal-ids test above still passed.
        """
        service = BudgetEnforcementService()

        entities = service._get_entity_hierarchy(self._context(HOSTED_WORKER, HUMAN))

        assert (EntityType.SERVICE_ACCOUNT, HOSTED_WORKER) in entities
        assert (EntityType.ROOT_USER, HUMAN) in entities

    def test_hosted_worker_identity_cannot_equal_a_canonical_users_id(self):
        """Why the skip is structurally unreachable for hosted runs, as an invariant.

        Stated as a property rather than a case: the shared worker identity is a fixed
        registry name and a root human id is a generated UUID, so no run can make the
        two sides of the comparison equal. This is what makes the guard inert on that
        path — and pinning it means a future change to either identity shape (a
        per-run agent identity, or a non-UUID root id) trips a test instead of quietly
        making the equal case reachable and unreviewed.
        """
        from src.budget.enforcement_service import _unqualify_root_principal_id

        assert HOSTED_WORKER != HUMAN
        # The comparison unqualifies first (#4344); neither form can alias the worker.
        assert _unqualify_root_principal_id(HUMAN) != HOSTED_WORKER
        assert _unqualify_root_principal_id(SERVICE_ROOT_ID) != HOSTED_WORKER
        # A canonical users.id is a UUID: hyphenated, hex, no colon. The worker name is
        # none of those, so the namespaces are disjoint by construction.
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", HUMAN)
        assert not re.fullmatch(r"[0-9a-f-]+", HOSTED_WORKER)


class TestPeriodRollover:
    """T16: cumulative *per period*, not cumulative forever."""

    @pytest.mark.asyncio
    async def test_headroom_resets_when_the_period_rolls(self, redis_client, clock):
        """``period_start`` is in both the ledger key and the Redis key.

        So at a period boundary the human's counter abandons the old key rather
        than inheriting its total. Proves the envelope is a real DAILY/WEEKLY/
        MONTHLY budget and not a rolling in-flight window.
        """
        from src.budget.utils import get_period_start_end
        from src.shared.schemas.budget import PeriodType

        ledger = _Ledger(budgets={(EntityType.ROOT_USER.value, HUMAN): "10.00"})
        service = _service(redis_client, clock, _chain_registry({"evt-1": "svc-agent-1"}))
        captured: list = []
        original = service._reserve_or_degrade

        async def spy(request_id, estimated_cost, targets):
            captured.extend(targets)
            return await original(request_id, estimated_cost, targets)

        with patch.object(service, "_reserve_or_degrade", spy):
            await _drive(
                service,
                ledger,
                _config(),
                context=_agent_context("svc-agent-1"),
                run_id="evt-1",
                request_id="req-1",
                body=b"{}",
            )

        root_targets = {t.period_type: t for t in captured if t.entity_type == EntityType.ROOT_USER.value}
        assert set(root_targets) == {"daily", "weekly", "monthly"}, "the human's line must be checked on every calendar period"

        for period_type, target in root_targets.items():
            expected_start, _ = get_period_start_end(PeriodType(period_type))
            assert target.period_start == expected_start.isoformat()
            # The period start is inside the Redis key, so a rollover cannot
            # inherit the previous period's spend.
            assert target.period_start in target.key()


class TestDependencyGate:
    """The envelope is inert until #4187's binding is flipped to enforce."""

    @pytest.mark.asyncio
    async def test_shipped_defaults_do_not_enforce_the_envelope(self, redis_client, clock):
        """With the shipped defaults (disabled / shadow), nothing is denied.

        The root-human value rides #4187's server-side binding, which withholds
        the binding in shadow mode. That coupling is deliberate and is the
        rollout gate for this feature — asserted so it cannot regress into
        enforcing before #4187 does.
        """
        ledger = _Ledger(budgets={(EntityType.ROOT_USER.value, HUMAN): "0.01"})
        service = _service(redis_client, clock, _chain_registry({"evt-1": "svc-agent-1"}))
        shipped = BudgetConfig()
        assert shipped.budget_run_cap_enabled is False
        assert shipped.budget_run_binding_mode == "shadow"

        harness = await _drive(
            service,
            ledger,
            shipped,
            context=_agent_context("svc-agent-1"),
            run_id="evt-1",
            request_id="req-1",
        )
        assert harness.status == 200


class TestEntityTypeContract:
    """The enum value is the cross-component contract (see T15, Lambda side)."""

    def test_root_user_value_is_the_wire_literal(self):
        """``root_user`` is what the tracker Lambda writes and this reads.

        A drift here is invisible at runtime: the config/usage lookups simply find
        no row and every request passes. That is exactly how the org line's
        ``"organization"``/``"org"`` mismatch (#4322) survived unnoticed.
        """
        assert EntityType.ROOT_USER.value == "root_user"
        assert EntityType.ROOT_USER != EntityType.USER
        # Fits budget_usage/budget_configs.entity_type String(20) — no migration.
        assert len(EntityType.ROOT_USER.value) <= 20


class TestChatLogWriteSites:
    """T17: every chat-log write site must carry the attribution forward.

    The settled ledger is fed exclusively by chat logs. ``proxy/routes.py`` has
    four write sites — streaming and non-streaming, on both the Bedrock and the
    Mantle path — and a request that lands on a site which drops the field is
    metered with no root-human row at all. That failure is invisible: the request
    succeeds, the reservation is released on completion, and the human's settled
    floor simply never rises, so the cap they are supposed to be under silently
    never binds for traffic on that path. Threading three of four is the same bug
    as threading none, just harder to notice.
    """

    @staticmethod
    def _routes_source() -> str:
        return (Path(__file__).resolve().parents[2] / "src" / "proxy" / "routes.py").read_text()

    def test_all_four_write_sites_pass_the_attribution(self):
        source = self._routes_source()

        # Locate the calls rather than trusting a global count: `root_human_id=`
        # appearing four times anywhere in the file would also satisfy a naive
        # assertion while leaving one call site bare.
        call_starts = [m.start() for m in re.finditer(r"(log_chat_async|create_streaming_logging_wrapper)\(", source)]
        # Exclude the import line, which is not a call.
        call_starts = [i for i in call_starts if "import" not in source[source.rfind("\n", 0, i) : i]]
        assert len(call_starts) == 4, f"expected 4 chat-log write sites, found {len(call_starts)}"

        for start in call_starts:
            # The call's own argument list: up to the next blank-line-delimited
            # statement boundary, which is well past the closing paren.
            body = source[start : start + 1200]
            end = body.find("\n        )")
            args = body[: end if end > 0 else len(body)]
            line_no = source[:start].count("\n") + 1
            assert "root_human_id=" in args, f"chat-log write site at routes.py:{line_no} drops root_human_id"
            # Must come off the server-resolved context field, not a header or a
            # request-body value the caller controls.
            assert "root_human_id=context.attributed_user_id" in args, (
                f"chat-log write site at routes.py:{line_no} sets root_human_id from something other than the server-resolved context"
            )

    def test_the_logging_service_accepts_and_persists_the_field(self):
        """The parameter has to survive the whole call chain, not just the entry.

        ``log_chat_async`` -> ``_log_chat_impl`` -> ``_build_chat_log`` -> the
        ``ChatLog`` model: a signature that accepts the kwarg and drops it before
        the model is the same silent metering gap as an unthreaded call site.
        """
        import inspect

        from src.chat_logging import service as logging_service
        from src.chat_logging.schemas import ChatLog

        for fn_name in ("log_chat_async", "_log_chat_impl", "_build_chat_log", "create_streaming_logging_wrapper"):
            fn = getattr(logging_service, fn_name, None) or getattr(logging_service.ChatLoggingService, fn_name)
            assert "root_human_id" in inspect.signature(fn).parameters, f"{fn_name} does not accept root_human_id"

        assert "root_human_id" in ChatLog.model_fields
        # Absent means absent, not a row keyed on a sentinel.
        assert ChatLog.model_fields["root_human_id"].default == ""

    @staticmethod
    def _chat_log(**overrides):
        from src.chat_logging.schemas import ChatLog, ChatLogRequest, ChatLogResponse, ScrubbingMetadata

        fields = {
            "request_id": "req-1",
            "timestamp": datetime.now(UTC),
            "org_id": TENANT,
            "user_id": "svc-agent-1",
            "account_type": "service",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "api_format": "bedrock",
            "latency_ms": 12.0,
            "request": ChatLogRequest(),
            "response": ChatLogResponse(),
            "scrubbing": ScrubbingMetadata(level="off"),
        }
        fields.update(overrides)
        return ChatLog(**fields)

    def test_the_field_reaches_the_serialised_log(self):
        """What the tracker Lambda actually reads is the serialised JSON."""
        assert self._chat_log(root_human_id=HUMAN).model_dump()["root_human_id"] == HUMAN

        # And a log without it serialises to the empty string the Lambda treats
        # as "no attribution" (falsy -> no ledger row), never to a literal "None"
        # or the string "null", either of which the Lambda would read as a real
        # entity id and write a shared bogus ledger line for.
        bare = self._chat_log(request_id="req-2", user_id="human-direct", account_type="human").model_dump()
        assert bare["root_human_id"] == ""
        assert not bare["root_human_id"]
        assert json.loads(self._chat_log().model_dump_json())["root_human_id"] == ""
