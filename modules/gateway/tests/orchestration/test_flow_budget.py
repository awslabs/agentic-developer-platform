"""One allowance for a whole accepted delivery (#5128, validation A1-3 / A1-4).

`test_policy_admission.py` proves the *settled* half of the spend bound: a total read
from `usage_logs` is compared against the owner's cap. That half cannot bound
concurrent admissions, because every one of them reads the same lagged total and
individually fits. This file covers the half that closes it — the live reservation —
and the two checklist items that describe it:

  - **A1-3.** Concurrent developer/reviewer/repair/evaluation runs under one flow
    share a single allowance, and a new run id, a retry or a restart cannot reset it.
  - **A1-4.** Unknown usage prevents new spend rather than reading as zero, and
    reconciliation gives back only headroom that is genuinely free.

Harness notes, inherited deliberately from `tests/budget/test_run_spend_cap.py` (the
#4187 suite) because the properties under test are properties of that machinery:

* The reservation Lua runs **for real** against `fakeredis` + `lupa`. A mocked store
  would mock the thing under test: all-or-nothing atomicity, the same-`request_id`
  replacement rule and the per-target deadline are exactly what these tests assert.
* Time is an **injected clock**, never `sleep`, so deadline expiry is deterministic.
* Config overrides use a **real** `BudgetConfig` with `object.__setattr__`, never a
  `MagicMock` — a fully-patched config asserts a guarantee it never exercised (the
  #4046 trap).
* The engine fixtures are imported from `test_policy_admission.py` rather than rebuilt,
  so a divergence between what admission does and what these tests exercise cannot
  hide in a differently-built fixture.

The arithmetic every test below rests on: the default policy allows `$50.00`, and one
admission reserves the worst case it could cost, `min(budget_run_cap_usd $25, $50)` =
`$25`. So a flow admits **two** concurrent actions and the third contends with them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from src.budget.config import BudgetConfig
from src.budget.reservations import ReservationStore
from src.orchestration import flow_budget
from src.orchestration.execution_policy import AcceptanceMode, DenyReason, flow_budget_binding
from src.orchestration.flow_budget import (
    admission_cost_usd,
    admission_request_id,
    flow_reservation_target,
    release_flow_admission,
    reserve_flow_admission,
)
from src.orchestration.models import NodeKind
from src.orchestration.state import NodeState
from src.shared.schemas.budget import EntityType, PeriodType
from tests.orchestration.test_policy_admission import (  # noqa: F401  (autouse fixture)
    FLOW_SLUG,
    ORG_A,
    _authorize,
    _fixture,
    _limits,
    _make_node,
    _make_usage,
    _policy,
    policy_budget_initializers,
    run_store,
)
from tests.orchestration.test_policy_admission import engine as engine_fixture
from tests.orchestration.test_policy_admission import session as session_fixture

# Re-exported under the plain names pytest resolves, following the alias pattern
# `tests/agentauth/test_graph_dispatch.py` uses for the same borrowed fixtures.
engine = engine_fixture
session = session_fixture

# The #4287 hierarchy deadline: a SIGKILL backstop for one in-flight HTTP request.
# A flow's counter must NOT inherit it — see `test_a_hold_outlives_the_hierarchy_ttl`.
RESERVATION_TTL = 120
FLOW_TTL = 86_400

# One admission's worst case under the default fixture policy.
ADMISSION = Decimal("25.00")


@pytest.fixture
def redis_client():
    """A real Lua-capable fake Redis, shared by every store in one test."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def clock():
    """Injected clock. Mutate `clock[0]` to advance time; never sleep."""
    return [1_000.0]


@pytest.fixture
def flow_store(monkeypatch, redis_client, clock) -> ReservationStore:
    """Point the admission path at the fake Redis.

    Patches the module-level store `get_flow_reservations` memoizes, which is how
    production resolves it — rather than threading a `store=` argument through
    `authorize_node_dispatch`, which would let these tests pass while the real call
    path reached a different store.

    Constructed with the **hierarchy** default TTL on purpose: every flow target sets
    its own, so a test that passed only because the store default was generous would
    be asserting nothing.
    """
    store = ReservationStore(
        redis_url=None,
        ttl_seconds=RESERVATION_TTL,
        clock=lambda: clock[0],
        client=redis_client,
    )
    monkeypatch.setattr(flow_budget, "_reservations", store)
    return store


def _budget_config(**overrides) -> BudgetConfig:
    """A REAL BudgetConfig with only the named fields overridden."""
    config = BudgetConfig()
    for name, value in overrides.items():
        object.__setattr__(config, name, value)
    return config


async def _keys(redis_client) -> list[str]:
    # This suite inspects dispatch holds. Model charges have their own accumulator
    # under the same stable flow binding, tested by test_flow_meter.py.
    return sorted(key for key in await redis_client.keys("*") if ":models:run:lifetime" not in key)


async def _holds(redis_client) -> dict[str, Decimal]:
    """Every live hold in the flow's hash, as `request_id -> amount`.

    Amount only: the deadline half is asserted separately by the TTL test, and
    including it here would make every assertion depend on the injected clock.
    """
    keys = await _keys(redis_client)
    assert len(keys) == 1, f"expected exactly one flow counter, found {keys}"
    raw = await redis_client.hgetall(keys[0])
    return {field: Decimal(value.split(":")[0]) for field, value in raw.items()}


# ---------------------------------------------------------------------------
# The key: what makes this a *flow* allowance rather than a per-run one
# ---------------------------------------------------------------------------


class TestTheBindingIsStableAndShared:
    """A1-3, at the unit boundary: one key, derived from the flow id alone."""

    def test_the_key_is_a_pure_function_of_the_flow(self) -> None:
        """No node, attempt, run id or wall-clock time appears in the key.

        This is the whole binding requirement in one assertion: the same flow
        resolves to the same counter, so a restarted, retried or newly-spawned
        descendant addresses the ledger its predecessors have been drawing down.
        """
        policy = _policy()
        first = flow_reservation_target(org_id=ORG_A, flow_id="flow-abc", policy=policy, settled_usd=Decimal(0))
        # A later admission of the same flow, after some spend has settled.
        second = flow_reservation_target(org_id=ORG_A, flow_id="flow-abc", policy=policy, settled_usd=Decimal("7.50"))

        assert first.key() == second.key()
        assert first.key() == f"budget:resv:{{{ORG_A}}}:flow:flow:flow-abc:run:lifetime"
        assert first.entity_type == EntityType.FLOW.value
        assert first.entity_id == flow_budget_binding("flow-abc")
        # Lifetime, not a calendar window: a delivery is not a month, and a rollover
        # would hand a long flow a second full allowance.
        assert first.period_type == PeriodType.RUN.value

    def test_two_flows_do_not_share_a_counter(self) -> None:
        policy = _policy()
        one = flow_reservation_target(org_id=ORG_A, flow_id="flow-1", policy=policy, settled_usd=Decimal(0))
        two = flow_reservation_target(org_id=ORG_A, flow_id="flow-2", policy=policy, settled_usd=Decimal(0))
        assert one.key() != two.key()

    def test_two_tenants_do_not_share_a_counter(self) -> None:
        """Cross-tenant leakage would let one org exhaust another's allowance.

        The org also has to be the Redis Cluster hash tag (the literal braces), or
        the multi-key Lua stops being valid on a cluster.
        """
        policy = _policy()
        mine = flow_reservation_target(org_id=ORG_A, flow_id="flow-1", policy=policy, settled_usd=Decimal(0))
        theirs = flow_reservation_target(org_id="org-beta", flow_id="flow-1", policy=policy, settled_usd=Decimal(0))
        assert mine.key() != theirs.key()
        assert mine.key().startswith(f"budget:resv:{{{ORG_A}}}:")

    def test_headroom_is_the_remainder_left_by_settled_spend(self) -> None:
        """A flow's denominator is `settled + in_flight`, so headroom is a remainder.

        Passing the full cap here — which is correct for run and chain, neither of
        which has a settled ledger — would discard the settled half and let a flow
        spend its allowance twice.
        """
        target = flow_reservation_target(
            org_id=ORG_A,
            flow_id="flow-1",
            policy=_policy(limits=_limits(max_spend_usd=Decimal("50.00"))),
            settled_usd=Decimal("30.00"),
        )
        assert target.headroom_usd == Decimal("20.00")

    def test_headroom_never_goes_negative(self) -> None:
        """Over-cap is a block in the rule; reaching here means it was skipped.

        Zero headroom denies everything, which is the right behaviour for that
        mistake — a negative number would be a nonsensical Lua comparison.
        """
        target = flow_reservation_target(
            org_id=ORG_A,
            flow_id="flow-1",
            policy=_policy(limits=_limits(max_spend_usd=Decimal("10.00"))),
            settled_usd=Decimal("25.00"),
        )
        assert target.headroom_usd == Decimal(0)

    def test_a_hold_is_keyed_on_the_node_not_the_attempt(self) -> None:
        """So a retry supersedes its own hold instead of opening a second one."""
        assert admission_request_id("node-7") == "orchnode:node-7"

    def test_one_admission_reserves_the_per_run_ceiling(self) -> None:
        """No per-node estimate exists, so the worst case is what is held.

        Reserving an optimistic figure would let N concurrent admissions each hold a
        fraction of what they then spend — a cap exceeded by work admitted against a
        number nobody was holding to.
        """
        assert admission_cost_usd(_policy(limits=_limits(max_spend_usd=Decimal("50.00")))) == Decimal("25.00")

    def test_a_small_policy_cannot_shrink_the_actual_run_ceiling(self) -> None:
        """A smaller reservation does not lower the actual per-run cap."""
        assert admission_cost_usd(_policy(limits=_limits(max_spend_usd=Decimal("4.00")))) == Decimal("25.00")


# ---------------------------------------------------------------------------
# A1-3: concurrent descendants share the allowance
# ---------------------------------------------------------------------------


class TestConcurrentAdmissionsShareOneAllowance:
    """**The property the settled ledger cannot provide.**

    Every node in these tests is `ready` with no usage row, so the settled total is
    `$0.00` and the pre-#5128 check would admit all of them. Any denial here therefore
    comes from the live reservation and nowhere else — which is what makes these tests
    fail against a `SUM(cost_usd) <= cap` implementation.
    """

    async def test_a_third_concurrent_action_is_refused(self, session, flow_store, redis_client) -> None:
        flow, first = await _fixture(session, policy=_policy())
        second = await _make_node(session, flow, node_ref="s8")
        third = await _make_node(session, flow, node_ref="s9")

        assert (await _authorize(session, first)).permitted
        assert (await _authorize(session, second)).permitted

        refused = await _authorize(session, third)
        assert not refused.permitted
        assert refused.reason is DenyReason.SPEND_LIMIT_EXCEEDED

        # Two holds against one counter — the shared-allowance property, stated as
        # state rather than inferred from the denial.
        assert await _holds(redis_client) == {
            admission_request_id(first.id): ADMISSION,
            admission_request_id(second.id): ADMISSION,
        }

    async def test_every_kind_of_descendant_draws_on_the_same_counter(self, session, flow_store, redis_client) -> None:
        """A reviewer and an evaluation contend with the developer run, not beside it.

        Separate counters per node kind is the shape this requirement exists to rule
        out: three actions each individually inside a $50 policy, spending $75.
        """
        flow, developer = await _fixture(
            session,
            # The eval is admissible only because the owner marked that specific
            # address for machine acceptance — the mode, not a blanket permission.
            policy=_policy(evaluation_acceptance={f"{FLOW_SLUG}/4191/wave-4/e1": AcceptanceMode.MACHINE}),
        )
        evaluation = await _make_node(session, flow, node_ref="e1", kind=NodeKind.EVAL.value)

        assert (await _authorize(session, developer)).permitted
        assert (await _authorize(session, evaluation)).permitted
        assert len(await _keys(redis_client)) == 1

        another = await _make_node(session, flow, node_ref="s8")
        assert not (await _authorize(session, another)).permitted

    async def test_a_retry_replaces_its_own_hold(self, session, flow_store, redis_client) -> None:
        """**The repair-loop failure this prevents.**

        Keyed on the attempt, attempt 2 would open a second field while attempt 1's
        sat there until the deadline — so a node that failed twice would have
        consumed the flow's entire allowance in holds for work no longer running,
        and the flow would stop dispatching with its ledger reading near zero.
        """
        flow, node = await _fixture(session, policy=_policy())

        assert (await _authorize(session, node)).permitted
        node.attempts = 1
        await session.flush()
        assert (await _authorize(session, node)).permitted

        assert await _holds(redis_client) == {admission_request_id(node.id): ADMISSION}

        # And the headroom the retry did not consume is really still there.
        sibling = await _make_node(session, flow, node_ref="s8")
        assert (await _authorize(session, sibling)).permitted

    async def test_a_restart_cannot_mint_a_new_allowance(self, session, flow_store, redis_client) -> None:
        """A restarted flow re-derives the same key, so the exhaustion persists.

        The engine sets each attempt's `correlation_id` to a fresh run id, which is
        why the chain scope was not reusable here: this test is what a chain-keyed
        implementation fails.
        """
        flow, first = await _fixture(session, policy=_policy())
        second = await _make_node(session, flow, node_ref="s8")
        assert (await _authorize(session, first)).permitted
        assert (await _authorize(session, second)).permitted

        # A restart: the tick runs again, nodes are re-examined, attempts advance.
        restarted = await _make_node(session, flow, node_ref="s9", attempts=1)
        refused = await _authorize(session, restarted)
        assert not refused.permitted
        assert refused.reason is DenyReason.SPEND_LIMIT_EXCEEDED
        assert len(await _keys(redis_client)) == 1

    async def test_a_hold_outlives_the_hierarchy_ttl(self, session, flow_store, redis_client, clock) -> None:
        """A flow counter on the 120s backstop would forget holds mid-delivery.

        An accumulator that forgets is not an accumulator: two minutes after each
        admission the allowance would silently refill, so the cap would bound
        concurrency for a moment and nothing thereafter.
        """
        flow, first = await _fixture(session, policy=_policy())
        assert (await _authorize(session, first)).permitted

        clock[0] += RESERVATION_TTL + 1
        second = await _make_node(session, flow, node_ref="s8")
        assert (await _authorize(session, second)).permitted

        third = await _make_node(session, flow, node_ref="s9")
        assert not (await _authorize(session, third)).permitted

        # Past the flow lifetime the abandoned holds are reaped, so a flow whose pods
        # all died cannot be wedged forever by holds nothing will ever release.
        clock[0] += FLOW_TTL
        assert (await _authorize(session, third)).permitted


# ---------------------------------------------------------------------------
# A1-4: unknown spend, and what reconciliation is allowed to give back
# ---------------------------------------------------------------------------


class TestUnknownSpendPreventsNewSpend:
    async def test_unreconciled_spend_blocks_before_anything_is_reserved(self, session, flow_store, redis_client) -> None:
        """Unknown is not zero — and the refusal happens *before* the reservation.

        Asserting Redis is untouched matters as much as the denial: a hold taken for
        an action that was then refused is released by nothing, because the caller
        never dispatches and no results pass ever reconciles it. Repeatedly retrying
        blocked work would starve the flow of its own allowance.
        """
        flow, node = await _fixture(session, policy=_policy())
        await _make_node(session, flow, node_ref="s8", state=NodeState.PASSED)  # ran, no usage row

        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.SPEND_UNKNOWN
        assert await _keys(redis_client) == []

    async def test_a_rule_denial_reserves_nothing(self, session, flow_store, redis_client) -> None:
        """Same starvation property, reached through a different refusal."""
        _, node = await _fixture(session, policy=_policy(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
        assert not (await _authorize(session, node)).permitted
        assert await _keys(redis_client) == []

    async def test_a_human_gate_consumes_no_allowance(self, session, flow_store, redis_client) -> None:
        """A gate is a human decision, not an autonomous action. It bills nothing and
        must hold nothing — otherwise waiting for a person would spend the budget."""
        flow, _ = await _fixture(session, policy=_policy())
        gate = await _make_node(session, flow, node_ref="g1", kind=NodeKind.GATE.value, issue_ref=None)
        assert not (await _authorize(session, gate)).permitted
        assert await _keys(redis_client) == []


class TestReconciliationRestoresOnlyValidHeadroom:
    async def test_a_settled_node_gives_its_hold_back(self, session, flow_store, redis_client) -> None:
        """**Why the release exists at all.**

        Without it a flow would admit only `max_spend_usd / run_cap` actions in its
        entire life — two, here — no matter how little each one actually cost. With
        it, a $1.00 action returns $24.00 of pessimism to the flow.

        The numbers are chosen so the release is load-bearing: settled $1.00 leaves
        $49.00 of headroom, and the stale hold plus this admission is $50.00. If the
        finished node's hold were kept, this permit would be a denial.
        """
        flow, done = await _fixture(session, policy=_policy())
        assert (await _authorize(session, done)).permitted

        # The node finishes and its cost reaches the settled ledger.
        done.state = NodeState.PASSED.value
        await session.flush()
        await _make_usage(session, done, cost_usd="1.00")

        successor = await _make_node(session, flow, node_ref="s8")
        assert (await _authorize(session, successor)).permitted

        assert await _holds(redis_client) == {
            # Released to ZERO, not adjusted to the $1.00 actual. The settled half of
            # the denominator is read from `usage_logs`, so writing the actual into
            # the live half too would count the same dollars twice and shrink the
            # allowance by every completed action's cost a second time.
            admission_request_id(done.id): Decimal(0),
            admission_request_id(successor.id): ADMISSION,
        }

    async def test_a_running_nodes_hold_is_never_released(self, session, flow_store, redis_client) -> None:
        """**The half-measure this rules out.**

        A run bills incrementally, so a mid-run usage row is a partial total and the
        run may still spend up to the per-run ceiling. Releasing on the presence of a
        row would leave that remainder bounded by nothing at all.
        """
        flow, node = await _fixture(session, policy=_policy())
        running = await _make_node(session, flow, node_ref="s8", state=NodeState.RUNNING)
        await _make_usage(session, running, cost_usd="1.00")
        await reserve_flow_admission(
            org_id=running.org_id,
            flow_id=running.flow_id,
            policy=_policy(),
            settled_usd=Decimal(0),
            node_id=running.id,
        )

        # Settled $1.00 → $49.00 headroom, and the running node's $25.00 hold stands,
        # so this admission's $25.00 does not fit.
        refused = await _authorize(session, node)
        assert not refused.permitted
        assert refused.reason is DenyReason.SPEND_LIMIT_EXCEEDED
        assert await _holds(redis_client) == {admission_request_id(running.id): ADMISSION}

    async def test_releasing_twice_is_not_a_double_credit(self, session, flow_store, redis_client) -> None:
        """The sweep runs on every admission, so it re-releases the same node
        repeatedly. `HSET` of the same zero is why that is safe."""
        flow, node = await _fixture(session, policy=_policy())
        assert (await _authorize(session, node)).permitted

        for _ in range(3):
            await release_flow_admission(
                org_id=node.org_id,
                flow_id=node.flow_id,
                policy=_policy(),
                settled_usd=Decimal("1.00"),
                node_id=node.id,
            )

        assert await _holds(redis_client) == {admission_request_id(node.id): Decimal(0)}

    async def test_an_expired_hold_is_not_revived_by_a_release(self, session, flow_store, redis_client, clock) -> None:
        """Its spend is already settling into Postgres; re-adding it would
        double-count and hold the flow below its true cap."""
        flow, node = await _fixture(session, policy=_policy())
        assert (await _authorize(session, node)).permitted

        clock[0] += FLOW_TTL + 1
        await release_flow_admission(
            org_id=node.org_id,
            flow_id=node.flow_id,
            policy=_policy(),
            settled_usd=Decimal("1.00"),
            node_id=node.id,
        )
        # Pruned rather than given a fresh deadline, and Redis drops the hash once its
        # last field goes — so the counter is gone entirely rather than holding a
        # revived $0.00 entry that a later reader would have to interpret.
        assert await _keys(redis_client) == []


# ---------------------------------------------------------------------------
# Unavailable reservation enforcement refuses new admissions
# ---------------------------------------------------------------------------


class TestUnavailableReservationsBlock:
    async def test_an_unreachable_redis_refuses_new_admission(self) -> None:
        """The accepted bound cannot be honored without concurrent holds."""
        client = MagicMock()
        client.register_script = MagicMock(return_value=AsyncMock(side_effect=ConnectionError("redis down")))
        store = ReservationStore(redis_url=None, ttl_seconds=RESERVATION_TTL, client=client)

        outcome = await reserve_flow_admission(
            org_id=ORG_A,
            flow_id="flow-1",
            policy=_policy(),
            settled_usd=Decimal(0),
            node_id="node-1",
            store=store,
        )
        assert not outcome.admitted
        assert outcome.degraded

    async def test_an_outage_does_not_relax_unknown_spend(self, session, monkeypatch, redis_client, clock) -> None:
        """The two conditions are different and must not be conflated.

        Degrading covers the *live* side being unreadable. An unreconciled node is
        the *settled* side, which the rule blocks on before the reservation is ever
        attempted — so a Redis outage cannot turn unknown spend into permitted spend.
        """
        client = MagicMock()
        client.register_script = MagicMock(return_value=AsyncMock(side_effect=ConnectionError("redis down")))
        monkeypatch.setattr(
            flow_budget,
            "_reservations",
            ReservationStore(redis_url=None, ttl_seconds=RESERVATION_TTL, clock=lambda: clock[0], client=client),
        )

        flow, node = await _fixture(session, policy=_policy())
        await _make_node(session, flow, node_ref="s8", state=NodeState.PASSED)

        decision = await _authorize(session, node)
        assert not decision.permitted
        assert decision.reason is DenyReason.SPEND_UNKNOWN

    async def test_no_backend_configured_refuses_policy_governed_work(self, session, monkeypatch) -> None:
        """Missing enforcement is distinguishable from an exhausted budget."""
        monkeypatch.setattr(flow_budget, "_reservations", ReservationStore(redis_url=None, ttl_seconds=RESERVATION_TTL))
        _, node = await _fixture(session, policy=_policy())
        assert (await _authorize(session, node)).reason is DenyReason.BUDGET_UNAVAILABLE

    async def test_the_rollback_lever_disables_reservations(self, session, monkeypatch, flow_store, redis_client) -> None:
        """Turning off reservations stops new bounded-policy admissions."""
        monkeypatch.setattr("src.budget.config.budget_config", _budget_config(budget_reservation_enabled=False))

        flow, first = await _fixture(session, policy=_policy())
        for ref in ("s8", "s9", "s10"):
            extra = await _make_node(session, flow, node_ref=ref)
            assert (await _authorize(session, extra)).reason is DenyReason.BUDGET_UNAVAILABLE
        assert (await _authorize(session, first)).reason is DenyReason.BUDGET_UNAVAILABLE
        assert await _keys(redis_client) == []
