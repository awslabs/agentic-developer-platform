"""Root-human attribution publishes in SHADOW mode — Issue #4591.

#4300 attributes an agent chain's spend to the initiating human: the budget
middleware resolves the run's server-side binding row, publishes the root human's
canonical id onto the request context, the proxy writes it into the chat log, and
the budget-usage-tracker Lambda accrues a ``root_user`` ledger row from it.
#4531/#4536 built the Budget Management authoring surface on that ledger.

The defect: ``_resolve_run_scope`` ended with ``return binding if enforcing else
None``, so a **successfully verified** binding was discarded in shadow mode — and
the publication site was gated on a non-``None`` binding. Shadow is the shipped
default, so in any environment still in shadow the entire #4300 → #4536 chain was
inert end to end: no cost record carried a root human, every per-person
cloud-agent budget displayed a cap that accrued nothing and enforced nothing.

The fix decouples the two questions the old line conflated:

* **"did this run id verify?"** — answered in both modes, and the ONLY source
  attribution may ever come from;
* **"may we deny on it yet?"** — the #4337 shadow-first rollout gate, which now
  governs the run/chain cap alone.

What this suite pins, and why each one is load-bearing:

* **The publication itself** in shadow, *with no run/chain reservation taken*.
  Both halves matter: publishing is the fix, and reserving would mean shadow had
  started to enforce and the #4337 gate was bypassed.
* **The two ``None`` dispositions stay ``None``.** A row that FAILED verification
  (forged/foreign/finished run id) and a lookup that never completed (DDB fault)
  must publish nothing. Widening either reopens the #4187/AD-1 forgery surface
  one field over: an agent could pin its spend on an arbitrary human.
* **The #4300 ordering contract.** ``_get_entity_hierarchy`` must run strictly
  after the publication or the ROOT_USER entity is silently never added — and
  every test that seeds the context directly still passes. Asserted by capturing
  the context state at the moment the hierarchy is built.
* **Enforce mode is unchanged.** Byte-identical behaviour, so the fix cannot have
  moved anything on the path that was already working.

Harness notes (inherited from ``test_root_human_envelope.py``, the #4300 suite):

* The reservation Lua runs **for real** against ``fakeredis`` + ``lupa``.
* Time is an **injected clock**, never ``sleep``.
* Config overrides use a **real** ``BudgetConfig`` with ``object.__setattr__`` on
  the fields under test, never a ``MagicMock`` — a fully-patched config asserts a
  guarantee it never exercised (the #4046 trap).
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
from botocore.exceptions import ClientError

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
# The initiating human, in the canonical `users.id` namespace — how the lineage
# plane writes root_human_id and how run_binding passes it through.
HUMAN = "3f2c1b90-0000-4000-8000-00000000abcd"
# The shared registry identity every hosted agent run authenticates as. Never a
# `users.id`, which is why the ROOT_USER dedup guard is a no-op on this path.
HOSTED_WORKER = "svc-agent-1"
RUN_ID = "evt-1"
CHAIN_ID = "chain-1"

# Issue #4344: a SERVICE-rooted root principal — the literal shape the
# webhook-ingress EventBridge handler writes into `root_human_id`.
SERVICE_KEY = "eventbridge:adp-dev-high-error-rate"
SERVICE_ROOT_ID = f"service:{SERVICE_KEY}"

# Roughly $0.75 of opus input plus the pricing module's output estimate.
_BIG_BODY = b"x" * 200_000


def _agent_context(user_id: str = HOSTED_WORKER, org_id: str = TENANT) -> TokenContext:
    """A hosted agent: its own service identity, no team/department.

    Deliberately does NOT seed ``attributed_user_id`` — resolving it server-side
    off the registry row is the code under test, so seeding it here would assert
    nothing (and hide the ordering trap).
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

    Defaults to the binding feature ON and mode SHADOW — the configuration this
    issue is about, and the one every environment ships in.
    """
    config = BudgetConfig()
    object.__setattr__(config, "budget_run_cap_enabled", True)
    object.__setattr__(config, "budget_run_binding_mode", "shadow")
    object.__setattr__(config, "budget_run_cap_ttl_seconds", RUN_TTL)
    for name, value in overrides.items():
        object.__setattr__(config, name, value)
    return config


class _Ledger:
    """A stub ledger answering by (entity_type, entity_id).

    Introspects the real SQLAlchemy statement's bound params rather than counting
    calls: this issue CHANGES how many queries run (shadow no longer reads the two
    run/chain cap rows), so a call-ordinal stub would answer the wrong question.

    ``budgets`` maps (entity_type, entity_id) -> cap. ``settled`` maps the same key
    -> already-spent. Anything absent reads "no budget configured".
    """

    def __init__(self, budgets: dict[tuple[str, str], str], settled: dict[tuple[str, str], str] | None = None):
        self._budgets = budgets
        self._settled = settled or {}
        # Every (entity_type, entity_id, org_id) the service asked a CAP question
        # about, in order. The run/chain cap lookups show up here, which is how a
        # test can assert that shadow mode never performed them. ``org_id`` is
        # captured because the run/chain cap is looked up for the tenant the ROW
        # names (#4337 B1) — asserting the type alone would not notice it moving.
        self.queried: list[tuple[str, str, str]] = []
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
            self.queried.append((entity_type, entity_id, params.get("org_id_1")))
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
        # Public so a test can mutate a row's `status` (the only attribute that
        # advances over a run's life — Issue #4337).
        self.rows = rows

    def query(self, **kwargs):
        run_id = kwargs["KeyConditionExpression"]._values[1]
        row = self.rows.get(run_id)
        return {"Items": [row] if row else []}


class _BrokenTable:
    """A registry that is down. Models the DDB lookup fault (degrade, never deny)."""

    def query(self, **kwargs):
        raise ClientError({"Error": {"Code": "InternalServerError"}}, "Query")


def _registry(
    root_human_id: str = HUMAN,
    *,
    tenant: str = TENANT,
    is_human_rooted: bool | None = True,
    owner: str = HOSTED_WORKER,
) -> _StubTable:
    """One row for ``RUN_ID``, owned by ``owner`` and rooted at ``root_human_id``.

    ``is_human_rooted`` defaults to ``True`` (a chain a PERSON set in motion).
    Pass ``False`` for a service-rooted run — the flag is what distinguishes the
    two kinds of principal sharing the ``root_human_id`` field (#4344).
    """
    return _StubTable(
        {
            RUN_ID: {
                "user_id": owner,
                "tenant_id": tenant,
                "root_human_id": root_human_id,
                "correlation_id": CHAIN_ID,
                "arrived_at": "2026-08-28T10:00:00Z",
                **({} if is_human_rooted is None else {"is_human_rooted": is_human_rooted}),
            }
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
        # This harness simulates an admitted provider call, not a free local response.
        scope["state"]["token_context"]._budget_provider_started = True
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"message":"success"}'})

    async def post(self, *, token_context, body, request_id, run_id):
        headers = [(b"content-length", str(len(body)).encode())]
        if run_id is not None:
            headers.append((b"x-agent-runid", run_id.encode()))

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
    run_id: str | None = RUN_ID,
    request_id: str = "req-1",
    body: bytes = _BIG_BODY,
) -> _Harness:
    harness = _Harness(service)
    with patch.object(service, "_get_session") as get_session:
        get_session.return_value.__aenter__ = AsyncMock(return_value=ledger.session)
        get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("src.budget.enforcement_service.budget_config", config):
            await harness.post(token_context=context, body=body, request_id=request_id, run_id=run_id)
    return harness


async def _scope_keys(redis_client) -> list[str]:
    """Every run/chain reservation key currently in Redis.

    These are the #4187 run/chain scopes — the enforce-only half. Their presence
    in shadow would mean the rollout gate had been bypassed.
    """
    keys = await redis_client.keys("*")
    return [k for k in keys if f":{EntityType.RUN.value}:" in k or f":{EntityType.CHAIN.value}:" in k]


# =============================================================================
# GATE — must fail on pre-#4591 code
# =============================================================================


class TestShadowModePublishesAttribution:
    """The fix: a verified binding attributes spend even while nothing is capped."""

    @pytest.mark.asyncio
    async def test_verified_binding_publishes_the_root_human_in_shadow(self, redis_client, clock):
        """GATE: shadow mode publishes ``attributed_user_id`` and denies nothing.

        Pre-#4591 ``_resolve_run_scope`` returned ``None`` here despite the row
        verifying, so the publication site never ran: the field stayed ``""``, the
        proxy wrote an empty ``root_human_id`` into the chat log, and the tracker
        Lambda skipped the ``root_user`` row. This is the assertion that fails on
        the old code.

        No budget rows exist at all, so the request is admitted — attribution is a
        LABEL, and it must be produced on a request nothing denies.
        """
        context = _agent_context()
        service = _service(redis_client, clock, _registry())

        harness = await _drive(service, _Ledger(budgets={}), _config(), context=context)

        assert harness.status == 200
        assert harness.app_invoked is True
        assert context.attributed_user_id == HUMAN, "shadow mode must publish the verified row's root human"

    @pytest.mark.asyncio
    async def test_run_cap_feature_off_still_publishes_attribution(self, redis_client, clock):
        """The run-cap FLAG being off must not starve attribution either.

        ``budget_run_cap_enabled`` ships ``False`` in config.py. Gating the
        publication on it is the #4591 defect one flag over: an environment that
        never enabled run caps would deploy this fix and still accrue nothing.
        With the flag off, a verified binding still attributes — and nothing
        cap-shaped runs: no reservation, no denial, even in enforce mode.
        """
        context = _agent_context()
        service = _service(redis_client, clock, _registry())

        harness = await _drive(
            service,
            _Ledger(budgets={}),
            _config(budget_run_cap_enabled=False, budget_run_binding_mode="enforce", budget_run_cap_usd=Decimal("0.000001")),
            context=context,
        )

        assert harness.status == 200
        assert harness.app_invoked is True
        assert context.attributed_user_id == HUMAN
        assert context._run_scope_reservations == [], "feature off must take no run/chain reservations"

    @pytest.mark.asyncio
    async def test_jwt_caller_never_receives_attribution(self, redis_client, clock):
        """A signed-in human replaying a live run id is not that run's payer.

        ``verify_row_matches_caller`` deliberately never compares caller
        identity, and the ROOT_USER dedup guard compares a Cognito sub against a
        canonical users.id — disjoint namespaces that never match — so publishing
        for JWT callers would debit one request twice: once as (USER, sub), once
        as (ROOT_USER, users.id). Attribution labels AGENT spend; agents are IAM.
        """
        human = _agent_context(user_id="cognito-sub-of-a-human")
        object.__setattr__(human, "auth_source", "jwt")
        object.__setattr__(human, "account_type", "human")
        service = _service(redis_client, clock, _registry())

        harness = await _drive(service, _Ledger(budgets={}), _config(), context=human)

        assert harness.status == 200
        assert human.attributed_user_id == ""
        assert all(entity_type != EntityType.ROOT_USER for entity_type, _ in service._get_entity_hierarchy(human))

    @pytest.mark.asyncio
    async def test_the_root_human_reaches_the_entity_hierarchy_in_shadow(self, redis_client, clock):
        """The published id is what the hierarchy and the ledger key agree on.

        Publishing the field but failing to add the entity would leave the same
        symptom the issue describes (a budget line at $0), so the entity — not just
        the field — is the property. Read off the real ``_get_entity_hierarchy``
        rather than re-deriving it.
        """
        context = _agent_context()
        service = _service(redis_client, clock, _registry())

        await _drive(service, _Ledger(budgets={}), _config(), context=context)

        assert (EntityType.ROOT_USER, HUMAN) in service._get_entity_hierarchy(context)

    @pytest.mark.asyncio
    async def test_shadow_takes_no_run_or_chain_reservation(self, redis_client, clock):
        """The other half: publishing must NOT drag the #4187 cap into shadow.

        The run/chain caps can deny with no settled ledger behind them, which is
        why they are gated on ``enforce``. If attribution had been decoupled by
        simply dropping the mode check, shadow would start reserving — and then
        denying — and the #4337 rollout gate would be bypassed. Asserted three
        ways: no reservation key, no published release targets (#4323), and no cap
        row was even read.
        """
        context = _agent_context()
        service = _service(redis_client, clock, _registry())
        ledger = _Ledger(budgets={})

        harness = await _drive(service, ledger, _config(budget_run_cap_usd=Decimal("0.01")), context=context)

        assert harness.status == 200, "a $0.01 run cap must not bite in shadow"
        assert await _scope_keys(redis_client) == [], "shadow mode reserved run/chain headroom"
        assert context._run_scope_reservations == [], "#4323 release targets are enforce-only"
        for entity_type in (EntityType.RUN.value, EntityType.CHAIN.value):
            assert not [q for q in ledger.queried if q[0] == entity_type], f"shadow mode read the {entity_type} cap"

    @pytest.mark.asyncio
    async def test_publication_precedes_the_hierarchy_build(self, redis_client, clock):
        """The #4300 ORDERING CONTRACT, pinned.

        ``_get_entity_hierarchy`` reads ``attributed_user_id`` and adds the
        ROOT_USER entity only if it is set. Built before publication, the entity is
        silently never added and everything still looks green — so the ordering is
        asserted at the moment of the call, not after the request.
        """
        context = _agent_context()
        service = _service(redis_client, clock, _registry())
        seen_at_call_time = []
        real = service._get_entity_hierarchy

        def spy(ctx):
            seen_at_call_time.append(ctx.attributed_user_id)
            return real(ctx)

        with patch.object(service, "_get_entity_hierarchy", side_effect=spy):
            await _drive(service, _Ledger(budgets={}), _config(), context=context)

        assert seen_at_call_time == [HUMAN], "the hierarchy was built before attribution was published"

    @pytest.mark.asyncio
    async def test_service_rooted_run_publishes_the_qualified_id(self, redis_client, clock):
        """Issue #4344 survives the decoupling: a service root keeps its namespace.

        The qualification happens at the publication site, so moving that site out
        from under the mode gate could plausibly have skipped it. A bare service key
        here would collide with the canonical ``users.id`` namespace.
        """
        context = _agent_context()
        service = _service(redis_client, clock, _registry(root_human_id=SERVICE_KEY, is_human_rooted=False))

        await _drive(service, _Ledger(budgets={}), _config(), context=context)

        assert context.attributed_user_id == SERVICE_ROOT_ID

    @pytest.mark.asyncio
    async def test_row_with_no_root_human_publishes_nothing(self, redis_client, clock):
        """Empty stays empty (#4300).

        Rows written before the lineage plane carry no root. A qualified empty
        (``"service:"``) would be a brand-new sentinel collapsing every
        unattributed request in a tenant into one shared bogus ledger line.
        """
        context = _agent_context()
        service = _service(redis_client, clock, _registry(root_human_id=""))

        harness = await _drive(service, _Ledger(budgets={}), _config(), context=context)

        assert harness.status == 200
        assert context.attributed_user_id == ""


# =============================================================================
# The forgery boundary — only a VERIFIED row may attribute
# =============================================================================


class TestOnlyVerifiedBindingsAttribute:
    """Attribution rides verification, never the assertion.

    Every case here is a run id the caller asserted that did NOT verify, or one
    whose verification never completed. All must be admitted (shadow denies
    nothing) AND publish nothing. Admitting while publishing would let an agent
    name any human as the payer of its spend — the #4187/AD-1 forgery surface,
    one field over.
    """

    @pytest.mark.asyncio
    async def test_forged_run_id_publishes_nothing_and_logs_drift(self, redis_client, clock):
        """An invented run id: drift recorded, nothing published, nothing denied."""
        context = _agent_context()
        service = _service(redis_client, clock, _registry())
        ledger = _Ledger(budgets={})

        harness = _Harness(service)
        with patch.object(service, "_get_session") as get_session:
            get_session.return_value.__aenter__ = AsyncMock(return_value=ledger.session)
            get_session.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("src.budget.enforcement_service.budget_config", _config()):
                with patch("src.budget.enforcement_service.emit_run_binding_drift") as emit:
                    await harness.post(
                        token_context=context,
                        body=_BIG_BODY,
                        request_id="req-1",
                        run_id="evt-invented",
                    )

        assert harness.status == 200, "shadow mode denies nothing"
        assert harness.app_invoked is True
        assert context.attributed_user_id == "", "an unverified run id must never attribute"
        emit.assert_called_once_with(reason=ANY, environment=ANY)

    @pytest.mark.asyncio
    async def test_run_belonging_to_another_tenant_publishes_nothing(self, redis_client, clock):
        """The tenant-scoping failure is the #4337 forge resistance.

        A row from another tenant carries another tenant's human. Publishing it
        would charge that stranger for this caller's spend — and in shadow there is
        no denial to make the attempt visible.
        """
        context = _agent_context()
        service = _service(
            redis_client,
            clock,
            _registry(root_human_id="a-different-human", tenant="org-somebody-else", owner="some-other-account"),
        )

        harness = await _drive(service, _Ledger(budgets={}), _config(), context=context)

        assert harness.status == 200
        assert context.attributed_user_id == ""

    @pytest.mark.asyncio
    async def test_finished_run_publishes_nothing(self, redis_client, clock):
        """Issue #4337 property 3: a completed run is not a live capability.

        Rotating across ids from one's own completed runs is the one direction an
        agent genuinely controls, so the liveness check has to hold on the
        attribution path too.
        """
        context = _agent_context()
        registry = _registry()
        registry.rows[RUN_ID]["status"] = "complete"
        service = _service(redis_client, clock, registry)

        harness = await _drive(service, _Ledger(budgets={}), _config(), context=context)

        assert harness.status == 200
        assert context.attributed_user_id == ""

    @pytest.mark.asyncio
    async def test_registry_outage_publishes_nothing_and_still_admits(self, redis_client, clock):
        """A DDB fault degrades to "no attribution", never to a guessed one.

        The pre-existing degrade contract (#4187) is unchanged: the hierarchy caps
        still ran, a caller cannot induce an outage selectively, so degrading costs
        nothing and denying costs everything.
        """
        context = _agent_context()
        service = _service(redis_client, clock, _BrokenTable())

        harness = await _drive(service, _Ledger(budgets={}), _config(), context=context)

        assert harness.status == 200, "a registry fault must not deny"
        assert harness.app_invoked is True
        assert context.attributed_user_id == ""

    @pytest.mark.asyncio
    async def test_no_run_id_publishes_nothing(self, redis_client, clock):
        """Direct traffic with no asserted run id is untouched (regression check).

        There is no binding to attribute from, and the caller's own USER line
        already covers their spend.
        """
        context = _agent_context()
        service = _service(redis_client, clock, _registry())

        harness = await _drive(service, _Ledger(budgets={}), _config(), context=context, run_id=None)

        assert harness.status == 200
        assert context.attributed_user_id == ""


# =============================================================================
# Enforce mode must be byte-identical to today
# =============================================================================


class TestEnforceModeUnchanged:
    """The path that already worked must not have moved."""

    @pytest.mark.asyncio
    async def test_enforce_still_publishes_and_still_reserves_run_scope(self, redis_client, clock):
        """Enforce mode does BOTH — attribution and the run/chain cap.

        The mirror of ``test_shadow_takes_no_run_or_chain_reservation``: the same
        three observations, all inverted.
        """
        context = _agent_context()
        service = _service(redis_client, clock, _registry())
        ledger = _Ledger(budgets={})

        harness = await _drive(service, ledger, _config(budget_run_binding_mode="enforce"), context=context)

        assert harness.status == 200
        assert context.attributed_user_id == HUMAN
        assert await _scope_keys(redis_client), "enforce mode must still reserve run/chain headroom"
        assert context._run_scope_reservations, "#4323 release targets must still be published"
        # `entity_id="*"` is the tenant-wide override shape, and the org_id is the
        # ROW's tenant (#4337 B1) — not the caller-influenced attributed org.
        assert (EntityType.RUN.value, "*", TENANT) in ledger.queried
        assert (EntityType.CHAIN.value, "*", TENANT) in ledger.queried

    @pytest.mark.asyncio
    async def test_enforce_still_denies_a_forged_run_id(self, redis_client, clock):
        """The 402 the rollout gate exists to hold back stays intact in enforce."""
        context = _agent_context()
        service = _service(redis_client, clock, _registry())

        harness = await _drive(
            service,
            _Ledger(budgets={}),
            _config(budget_run_binding_mode="enforce"),
            context=context,
            run_id="evt-invented",
        )

        assert harness.status == 402
        assert harness.app_invoked is False
        assert context.attributed_user_id == ""

    @pytest.mark.asyncio
    async def test_enforce_still_applies_the_run_cap(self, redis_client, clock):
        """A tiny run cap still bites in enforce — the #4187 denial is untouched.

        A FRESH context per request, deliberately: the run cap accumulates on the
        run's own Redis key across requests, not on anything the context carries.
        """
        service = _service(redis_client, clock, _registry())

        statuses = []
        for i in range(6):
            harness = await _drive(
                service,
                _Ledger(budgets={}),
                _config(budget_run_binding_mode="enforce", budget_run_cap_usd=Decimal("1.00")),
                context=_agent_context(),
                request_id=f"req-{i}",
            )
            statuses.append(harness.status)
            if harness.status == 402:
                break

        assert 402 in statuses, f"the run cap stopped enforcing: {statuses}"
        assert statuses[0] == 200, "a cap that denies from the first request is not a cap"


# =============================================================================
# The deliberate consequence, stated as a test
# =============================================================================


class TestAuthoredRootUserCapNowEnforces:
    """An authored per-person cap enforces in shadow. This is intended (#4591).

    Stated explicitly because it is the one behaviour change beyond "spend becomes
    visible", and an operator-visible one. The distinction that makes it correct:

    * the #4187 run/chain caps are PLATFORM DEFAULTS that deny with no settled
      ledger behind them, so they get the shadow-first rollout gate;
    * a ``root_user`` cap is a number a human deliberately typed into the Budget
      Management screen (#4536), on a settled Postgres ledger, with
      ``cap - settled`` headroom. It is hierarchy enforcement under
      ``budget_check_enabled``, exactly like the user and org caps beside it — and
      enforcing it is what that screen already promises.
    """

    @pytest.mark.asyncio
    async def test_authored_cap_denies_in_shadow_naming_the_root_user_scope(self, redis_client, clock):
        """The operator's $-cap stops the chain, and the denial names their line.

        The scope matters: an operator told to raise an *org* budget would change
        the wrong knob (see stopReason.ts / agent-worker.ts).
        """
        ledger = _Ledger(budgets={(EntityType.ROOT_USER.value, HUMAN): "0.01"})
        service = _service(redis_client, clock, _registry())

        harness = await _drive(service, ledger, _config(), context=_agent_context())

        assert harness.status == 402
        assert harness.app_invoked is False

        # The denial must NAME the person's line — the assertion this test is
        # titled for. The agent worker classifies the stop by matching `scope`
        # in the 402 body; without it the stop misreports as a hierarchy cap
        # and the operator is pointed at the org budget.
        body = json.loads(next(m["body"] for m in harness.messages if m["type"] == "http.response.body"))
        assert body["details"]["scope"] == "root_user"
        assert body["details"]["entity_type"] == "root_user"

    @pytest.mark.asyncio
    async def test_no_authored_cap_means_no_denial(self, redis_client, clock):
        """The premise of the test above: it is the CAP that denies, not the fix.

        Without this, the assertion above could pass for the wrong reason.
        """
        service = _service(redis_client, clock, _registry())

        harness = await _drive(service, _Ledger(budgets={}), _config(), context=_agent_context())

        assert harness.status == 200
        assert harness.app_invoked is True
