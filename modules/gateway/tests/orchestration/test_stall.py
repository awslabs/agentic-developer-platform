"""Tests for stall/halt detection and the defect-cycle bound (issue #4211).

Covers every case in the issue's Validation section. Four of them are adversarial
and load-bearing rather than confirmatory, and they are the reason this file
exists:

- **Threshold-below-deadline** (`TestThresholdBelowPodDeadline`): constructing a
  config where the stall threshold is not strictly below the agent pod deadline
  must FAIL. This is the test that prevents the original bug — pod dies first,
  node looks `running` forever, nobody diagnoses it — from being reintroduced by a
  later config change or by someone raising the deadline.
- **Bound-below-chain-depth** (`TestBoundBelowChainDepth`): a bound at or above
  `MAX_CHAIN_DEPTH` must be rejected at load, so the diagnosable `halted` always
  wins the race against the opaque dispatch-guard failure.
- **Engine cannot self-clear a halt** (`TestEngineCannotSelfClearAHalt`): asserted
  at source level *and* behaviourally. A bound the engine can lift is decorative.
- **No direct UPDATE** (`TestNoDirectStateUpdate`): AST-level, so it holds against a
  future refactor rather than only against today's code.

The SQLite fixture is copied from `test_tick.py` rather than shared: the two
pysqlite hooks are what make a second session see the first one's uncommitted
writes the way the deployed asyncpg path does, and the concurrency test depends on
that. Notification is a recording double throughout, so "was a human told?" is a
real assertion about publish calls and not about a log line.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration import stall as stall_module
from src.orchestration.models import (
    DecisionKind,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.notify import NotificationError, NotifyConfig
from src.orchestration.stall import (
    AGENT_POD_DEADLINE_SECONDS,
    DEFAULT_DEFECT_CYCLE_BOUND,
    MAX_CHAIN_DEPTH,
    StallConfig,
    StallConfigError,
    StallReport,
    detect_stalls,
)
from src.orchestration.state import ActorKind, NodeState, transition
from src.shared.models.base import Base

ORG_A = "org-alpha"
ORG_B = "org-beta"

# A fixed "now" so every elapsed-time assertion is deterministic.
NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)

TOPIC = "arn:aws:sns:us-east-1:123456789012:adp-dev-orchestration-alerts"


@pytest.fixture
async def engine():
    """In-memory SQLite shared across sessions. Fixture shape from `test_tick.py`."""
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(eng.sync_engine, "begin")
    def _emit_explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s


class _RecordingSNS:
    """Records publishes so "a human was told" is an assertion, not a log grep."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.publishes: list[dict] = []
        self._fail_with = fail_with

    def publish(self, **kwargs):
        if self._fail_with is not None:
            raise self._fail_with
        self.publishes.append(kwargs)
        return {"MessageId": f"msg-{len(self.publishes)}"}


@pytest.fixture
def sns(monkeypatch):
    client = _RecordingSNS()
    monkeypatch.setattr("src.orchestration.notify._sns_client", lambda _region: client)
    return client


@pytest.fixture
def notify_config():
    """A configured target, so tests exercise delivery rather than the disabled path."""
    return NotifyConfig(topic_arn=TOPIC, aws_region="us-east-1")


async def _make_flow(session: AsyncSession, *, org_id: str = ORG_A, slug: str = "flow-1") -> OrchestrationFlow:
    flow = OrchestrationFlow(org_id=org_id, slug=slug, title="Demo flow")
    session.add(flow)
    await session.flush()
    return flow


async def _make_node(
    session: AsyncSession,
    flow: OrchestrationFlow,
    *,
    node_ref: str,
    state: NodeState | str = NodeState.RUNNING,
    running_for: timedelta | None = None,
    attempts: int = 0,
    org_id: str | None = None,
    kind: str = "story",
) -> OrchestrationNode:
    """Create a node that entered its current state `running_for` ago.

    `updated_at` is what detection measures from — `apply_guarded_transition` sets
    it on every state write, so for a `running` node it is when it started running.
    """
    since = NOW - (running_for if running_for is not None else timedelta(seconds=0))
    node = OrchestrationNode(
        org_id=org_id or flow.org_id,
        flow_id=flow.id,
        epic_ref="4191",
        wave_ref="wave-4",
        node_ref=node_ref,
        kind=kind,
        title=f"Node {node_ref}",
        state=state.value if isinstance(state, NodeState) else state,
        attempts=attempts,
        created_at=since,
        updated_at=since,
    )
    session.add(node)
    await session.flush()
    return node


async def _state_of(session: AsyncSession, node_id: str) -> str:
    return (await session.execute(select(OrchestrationNode.state).where(OrchestrationNode.id == node_id))).scalar_one()


async def _decisions(session: AsyncSession, node_id: str) -> list[OrchestrationDecision]:
    stmt = select(OrchestrationDecision).where(OrchestrationDecision.node_id == node_id)
    return list((await session.execute(stmt)).scalars().all())


# A comfortable margin past the default threshold (0.9 * 6 h = 5.4 h).
PAST_THRESHOLD = timedelta(seconds=AGENT_POD_DEADLINE_SECONDS)
WELL_INSIDE_THRESHOLD = timedelta(seconds=60)


class TestThresholdBelowPodDeadline:
    """The invariant that prevents the original bug (R-O4a).

    If the stall threshold were longer than the agent pod's own deadline, the pod
    would always be killed first, the node would sit in `running` forever, and the
    engine would never call it stalled. These tests make that configuration
    impossible to construct.
    """

    def test_default_threshold_is_strictly_below_the_pod_deadline(self):
        config = StallConfig()
        assert config.stall_threshold_seconds < config.pod_deadline_seconds

    def test_the_relationship_is_pinned_so_raising_the_deadline_cannot_invert_it(self):
        # Derived, not hardcoded: a larger deadline must produce a larger threshold
        # that is STILL below it. This is what makes the invariant survive a change
        # to the Terraform value rather than only holding for today's numbers.
        for deadline in (600, 3_600, AGENT_POD_DEADLINE_SECONDS, 86_400, 1_000_000):
            config = StallConfig(pod_deadline_seconds=deadline)
            assert config.stall_threshold_seconds < deadline, f"threshold must stay below a {deadline}s deadline"

    def test_pod_deadline_mirrors_the_terraform_value(self):
        # `agent_pod_deadline_seconds` in
        # modules/agent-factory/webhook-ingress/infra/variables.tf. Pinned so the
        # two cannot drift silently; if Terraform changes, this test is the thing
        # that says so.
        assert AGENT_POD_DEADLINE_SECONDS == 21_600, "6h — must match Terraform's agent_pod_deadline_seconds"

    def test_threshold_fraction_at_or_above_one_is_rejected(self):
        # A fraction of 1.0 derives a threshold EQUAL to the deadline, which is the
        # inversion this story exists to prevent. "Strictly below" means 1.0 fails.
        for fraction in (1.0, 1.5, 2.0):
            with pytest.raises(StallConfigError, match="strictly between 0 and 1"):
                StallConfig(threshold_fraction=fraction)

    def test_absolute_threshold_at_or_above_the_deadline_is_rejected(self):
        # The override path must be checked too — otherwise an explicit
        # threshold_seconds buys its way past the invariant the fraction enforces.
        for threshold in (AGENT_POD_DEADLINE_SECONDS, AGENT_POD_DEADLINE_SECONDS + 1, 86_400):
            with pytest.raises(StallConfigError, match="STRICTLY BELOW the agent pod"):
                StallConfig(threshold_seconds=threshold)

    def test_lowering_the_deadline_below_an_absolute_threshold_is_rejected(self):
        # The realistic future regression: someone lowers the pod deadline and an
        # existing absolute threshold silently becomes longer than it.
        with pytest.raises(StallConfigError, match="STRICTLY BELOW the agent pod"):
            StallConfig(pod_deadline_seconds=600, threshold_seconds=1_200)

    def test_a_non_positive_deadline_is_rejected(self):
        for deadline in (0, -1):
            with pytest.raises(StallConfigError, match="must be positive"):
                StallConfig(pod_deadline_seconds=deadline)

    def test_the_24h_staleness_cutoff_is_not_reused(self):
        # `ACTIVE_STALENESS_HOURS = 24` in src/activity/liveness.py is four times
        # the pod deadline — exactly the inverted relationship. Constructing it must
        # fail rather than being quietly accepted.
        with pytest.raises(StallConfigError):
            StallConfig(threshold_seconds=24 * 3_600)


class TestBoundBelowChainDepth:
    """The cycle bound must lose to nothing and win against MAX_CHAIN_DEPTH."""

    def test_default_bound_is_three(self):
        assert StallConfig().defect_cycle_bound == DEFAULT_DEFECT_CYCLE_BOUND == 3

    def test_default_bound_is_strictly_below_max_chain_depth(self):
        assert StallConfig().defect_cycle_bound < MAX_CHAIN_DEPTH

    def test_max_chain_depth_mirrors_the_spawn_persona_guard(self):
        # `MAX_CHAIN_DEPTH` in webhook-ingress/lambda/common/spawn_persona.py.
        # Pinned on both sides so a change to either fails a test rather than
        # silently reordering the two limits.
        assert MAX_CHAIN_DEPTH == 8

    def test_bound_at_or_above_chain_depth_is_rejected_at_load(self):
        # If the bound were the looser limit, EVERY runaway defect would surface as
        # the opaque dispatch-guard failure instead of a diagnosable `halted`.
        for bound in (MAX_CHAIN_DEPTH, MAX_CHAIN_DEPTH + 1, 100):
            with pytest.raises(StallConfigError, match="STRICTLY BELOW MAX_CHAIN_DEPTH"):
                StallConfig(defect_cycle_bound=bound)

    def test_bound_is_configurable_below_the_limit(self):
        # The bound is configuration, not a call-site constant — but only within the
        # invariant.
        assert StallConfig(defect_cycle_bound=MAX_CHAIN_DEPTH - 1).defect_cycle_bound == 7
        assert StallConfig(defect_cycle_bound=1).defect_cycle_bound == 1

    def test_bound_below_one_is_rejected(self):
        for bound in (0, -1):
            with pytest.raises(StallConfigError, match="at least 1"):
                StallConfig(defect_cycle_bound=bound)


class TestStallDetection:
    """AC-7: a node past the threshold is detected, transitioned, and notified once."""

    async def test_node_past_the_threshold_is_detected_and_transitioned(self, session, sns, notify_config):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", running_for=PAST_THRESHOLD)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.stalls_detected == 1
        assert report.success
        # `failed`, not `halted`: a stall is human-recoverable and `attempts` is
        # preserved, so the cycle bound still applies when someone resumes it.
        assert await _state_of(session, node.id) == NodeState.FAILED.value

    async def test_a_notification_is_actually_delivered(self, session, sns, notify_config):
        # R-Q9d: the assertion is a publish call, not a log line.
        flow = await _make_flow(session)
        await _make_node(session, flow, node_ref="a", running_for=PAST_THRESHOLD)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.notifications_sent == 1
        assert len(sns.publishes) == 1
        assert sns.publishes[0]["TopicArn"] == TOPIC
        assert sns.publishes[0]["MessageAttributes"]["event"]["StringValue"] == "node_stalled"

    async def test_repeated_ticks_do_not_re_notify(self, session, sns, notify_config):
        # The once-only guarantee. It is structural: after the first pass the node is
        # `failed`, which is not a candidate state, so it is never re-examined.
        flow = await _make_flow(session)
        await _make_node(session, flow, node_ref="a", running_for=PAST_THRESHOLD)

        first = await detect_stalls(session, NOW, notify_config=notify_config)
        second = await detect_stalls(session, NOW + timedelta(hours=1), notify_config=notify_config)
        third = await detect_stalls(session, NOW + timedelta(hours=2), notify_config=notify_config)

        assert first.notifications_sent == 1
        assert second.notifications_sent == 0
        assert third.notifications_sent == 0
        assert second.nodes_examined == 0, "a failed node is no longer a candidate"
        assert len(sns.publishes) == 1, "exactly one notification per event, not one per tick"

    async def test_the_stall_is_recorded_with_a_reason(self, session, sns, notify_config):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", running_for=PAST_THRESHOLD)

        await detect_stalls(session, NOW, notify_config=notify_config)

        decisions = await _decisions(session, node.id)
        assert [d.kind for d in decisions] == [DecisionKind.NODE_STALLED.value]
        assert "stalled" in decisions[0].reason
        # The threshold and the deadline are both in the reason, so an operator can
        # tell whether the threshold was wrong without reading the code.
        assert str(StallConfig().stall_threshold_seconds) in decisions[0].reason
        assert decisions[0].from_state == NodeState.RUNNING.value
        assert decisions[0].to_state == NodeState.FAILED.value
        assert decisions[0].actor_kind == ActorKind.SERVICE.value

    async def test_a_concurrent_pass_produces_exactly_one_notification(self, session_factory, sns, notify_config):
        # Two overlapping passes both observe `running`; the conditional UPDATE means
        # exactly one matches a row. The loser must notify NOBODY — notifying there
        # is precisely the duplicate the once-only requirement forbids.
        async with session_factory() as setup:
            flow = await _make_flow(setup)
            node = await _make_node(setup, flow, node_ref="a", running_for=PAST_THRESHOLD)
            await setup.commit()

        async with session_factory() as first, session_factory() as second:
            first_report = await detect_stalls(first, NOW, notify_config=notify_config)
            await first.commit()
            second_report = await detect_stalls(second, NOW, notify_config=notify_config)
            await second.commit()

        assert first_report.stalls_detected == 1
        assert second_report.stalls_detected == 0
        assert len(sns.publishes) == 1
        async with session_factory() as check:
            assert await _state_of(check, node.id) == NodeState.FAILED.value


class TestDefectCycleBound:
    """AC-8: at the bound a defect halts with a reason; at bound-1 it does not."""

    async def test_at_the_bound_the_node_halts(self, session, sns, notify_config):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", attempts=DEFAULT_DEFECT_CYCLE_BOUND)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.halts_detected == 1
        assert await _state_of(session, node.id) == NodeState.HALTED.value

    async def test_at_bound_minus_one_it_does_not_halt(self, session, sns, notify_config):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", attempts=DEFAULT_DEFECT_CYCLE_BOUND - 1)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.halts_detected == 0
        assert await _state_of(session, node.id) == NodeState.RUNNING.value
        assert len(sns.publishes) == 0

    async def test_the_halt_records_a_reason_naming_attempts_and_the_bound(self, session, sns, notify_config):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", attempts=4)

        await detect_stalls(session, NOW, notify_config=notify_config)

        decisions = await _decisions(session, node.id)
        assert [d.kind for d in decisions] == [DecisionKind.NODE_HALTED.value]
        assert "4 attempt(s)" in decisions[0].reason
        assert f"bound of {DEFAULT_DEFECT_CYCLE_BOUND}" in decisions[0].reason
        assert decisions[0].to_state == NodeState.HALTED.value

    async def test_the_bound_is_honoured_when_configured(self, session, sns, notify_config):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", attempts=5)

        report = await detect_stalls(session, NOW, config=StallConfig(defect_cycle_bound=6), notify_config=notify_config)

        assert report.halts_detected == 0, "5 attempts is under a bound of 6"
        assert await _state_of(session, node.id) == NodeState.RUNNING.value

    async def test_a_halt_is_notified(self, session, sns, notify_config):
        flow = await _make_flow(session)
        await _make_node(session, flow, node_ref="a", attempts=DEFAULT_DEFECT_CYCLE_BOUND)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.notifications_sent == 1
        assert sns.publishes[0]["MessageAttributes"]["event"]["StringValue"] == "node_halted"

    async def test_halt_wins_over_stall_when_both_apply(self, session, sns, notify_config):
        # A node that has both exhausted its bound AND run too long must halt, not
        # merely fail — treating it as a stall would let it be resumed straight back
        # into the cycle it just exhausted, which is how the bound becomes
        # decorative.
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", running_for=PAST_THRESHOLD, attempts=DEFAULT_DEFECT_CYCLE_BOUND)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.halts_detected == 1
        assert report.stalls_detected == 0
        assert await _state_of(session, node.id) == NodeState.HALTED.value
        assert len(sns.publishes) == 1, "one event, one notification"

    async def test_a_node_awaiting_a_gate_can_still_halt(self, session, sns, notify_config):
        # A defect parked at a gate has still exhausted its budget.
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", state=NodeState.AWAITING_GATE, attempts=DEFAULT_DEFECT_CYCLE_BOUND)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.halts_detected == 1
        assert await _state_of(session, node.id) == NodeState.HALTED.value


class TestGuardedWriteOutcomes:
    """The two non-success outcomes of the guarded write are told apart.

    An authority rejection is a recorded deviation; a lost race is normal overlap.
    Neither may notify, and neither may be counted as a detection — conflating them
    would make either invisible.
    """

    async def test_an_authority_rejection_notifies_nobody_and_is_counted(self, session, sns, notify_config, monkeypatch):
        # Simulate `transition()` refusing the edge. The node must not move, nobody
        # must be told, and the refusal must reach the metrics as a rejection rather
        # than as a detection.
        async def refuse(*_args, **_kwargs):
            return 0, False

        monkeypatch.setattr(stall_module, "apply_guarded_transition", refuse)

        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", running_for=PAST_THRESHOLD)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.transitions_rejected == 1
        assert report.stalls_detected == 0
        assert report.notifications_sent == 0
        assert len(sns.publishes) == 0
        assert await _state_of(session, node.id) == NodeState.RUNNING.value

    async def test_a_lost_race_notifies_nobody_and_is_not_a_detection(self, session, sns, notify_config, monkeypatch):
        # Allowed, but 0 rows matched — a concurrent pass got there first.
        # Notifying here is exactly the duplicate the once-only rule forbids.
        async def lose_race(*_args, **_kwargs):
            return 0, True

        monkeypatch.setattr(stall_module, "apply_guarded_transition", lose_race)

        flow = await _make_flow(session)
        await _make_node(session, flow, node_ref="a", running_for=PAST_THRESHOLD)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.lost_races == 1
        assert report.stalls_detected == 0
        assert report.notifications_sent == 0
        assert len(sns.publishes) == 0
        # A lost race is normal overlap, not a failure.
        assert report.success

    async def test_no_decision_row_is_written_when_the_write_did_not_take(self, session, sns, notify_config, monkeypatch):
        # A decision row for a transition that never happened would be a false
        # record in an append-only table.
        async def lose_race(*_args, **_kwargs):
            return 0, True

        monkeypatch.setattr(stall_module, "apply_guarded_transition", lose_race)

        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", running_for=PAST_THRESHOLD)

        await detect_stalls(session, NOW, notify_config=notify_config)

        assert await _decisions(session, node.id) == []


class TestNoFalsePositives:
    """The engine must not become the thing that breaks the loop."""

    async def test_a_node_inside_the_threshold_is_not_flagged(self, session, sns, notify_config):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", running_for=WELL_INSIDE_THRESHOLD)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.stalls_detected == 0
        assert await _state_of(session, node.id) == NodeState.RUNNING.value
        assert len(sns.publishes) == 0

    async def test_a_node_exactly_at_the_threshold_is_not_flagged(self, session, sns, notify_config):
        # Strictly greater than, so the boundary is not a stall. Healthy work that
        # happens to land exactly on the threshold must survive.
        flow = await _make_flow(session)
        config = StallConfig()
        node = await _make_node(session, flow, node_ref="a", running_for=timedelta(seconds=config.stall_threshold_seconds))

        report = await detect_stalls(session, NOW, config=config, notify_config=notify_config)

        assert report.stalls_detected == 0
        assert await _state_of(session, node.id) == NodeState.RUNNING.value

    async def test_long_running_but_healthy_work_below_the_threshold_survives(self, session, sns, notify_config):
        # 3 h into a 6 h pod is normal for a long deploy orchestrator, not a stall.
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", running_for=timedelta(hours=3))

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.stalls_detected == 0
        assert await _state_of(session, node.id) == NodeState.RUNNING.value

    @pytest.mark.parametrize(
        "terminal_state",
        [NodeState.PASSED, NodeState.FAILED, NodeState.HALTED, NodeState.REJECTED_AT_GATE, NodeState.SUPERSEDED],
    )
    async def test_a_node_in_a_terminal_state_is_never_flagged(self, session, sns, notify_config, terminal_state):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", state=terminal_state, running_for=timedelta(days=30), attempts=99)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.stalls_detected == 0
        assert report.halts_detected == 0
        assert report.nodes_examined == 0, "a terminal node is not even a candidate"
        assert await _state_of(session, node.id) == terminal_state.value
        assert len(sns.publishes) == 0

    @pytest.mark.parametrize("state", [NodeState.PENDING, NodeState.READY])
    async def test_a_node_that_has_not_started_is_not_stalled(self, session, sns, notify_config, state):
        # `pending`/`ready` nodes are waiting on the tick, not wedged mid-execution.
        # Calling them stalled would flag the entire un-dispatched graph.
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", state=state, running_for=timedelta(days=7))

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.stalls_detected == 0
        assert await _state_of(session, node.id) == state.value

    async def test_a_node_awaiting_a_gate_is_not_stalled_by_elapsed_time(self, session, sns, notify_config):
        # A gate waits on a human by design. Flagging it would report every
        # unreviewed gate as a fault.
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", state=NodeState.AWAITING_GATE, running_for=timedelta(days=7))

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.stalls_detected == 0
        assert await _state_of(session, node.id) == NodeState.AWAITING_GATE.value

    async def test_an_undeclared_state_is_never_a_candidate(self, session, sns, notify_config):
        # Defence in depth, layer 1: the query only selects watched states, so a
        # node carrying a literal outside the vocabulary is not even fetched.
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a")
        await session.execute(OrchestrationNode.__table__.update().where(OrchestrationNode.id == node.id).values(state="mystery_state"))
        await session.flush()

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.nodes_examined == 0
        assert report.stalls_detected == 0
        assert await _state_of(session, node.id) == "mystery_state"

    def test_the_state_guard_rejects_an_undeclared_literal_and_says_so(self, caplog):
        # Defence in depth, layer 2: the guard itself, tested directly. The query
        # filter above is an optimisation; THIS is the guarantee, and it must hold
        # even if a future change widens the query. A node carrying a value outside
        # the vocabulary must never be transitioned on the strength of a guess.
        with caplog.at_level("ERROR"):
            assert stall_module._is_candidate_state("mystery_state") is None

        assert any("undeclared state" in rec.message for rec in caplog.records)

    def test_the_state_guard_rejects_every_terminal_state(self):
        for state in (NodeState.PASSED, NodeState.HALTED, NodeState.SUPERSEDED, NodeState.REJECTED_AT_GATE, NodeState.FAILED):
            assert stall_module._is_candidate_state(state.value) is None, f"{state} is terminal and must never be flagged"

    def test_the_state_guard_rejects_states_that_have_not_started(self):
        for state in (NodeState.PENDING, NodeState.READY):
            assert stall_module._is_candidate_state(state.value) is None

    def test_the_state_guard_accepts_the_watched_states(self):
        assert stall_module._is_candidate_state(NodeState.RUNNING.value) is NodeState.RUNNING
        assert stall_module._is_candidate_state(NodeState.AWAITING_GATE.value) is NodeState.AWAITING_GATE


class TestEngineCannotSelfClearAHalt:
    """R-Q9c: only an explicit human override resumes a halted node."""

    def test_no_engine_code_path_proposes_halted_to_ready(self):
        # Source-level. `stall.py` must never name READY as a transition target at
        # all — that is the only way it could clear a halt.
        tree = ast.parse(Path(inspect.getfile(stall_module)).read_text())

        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg != "to_state":
                continue
            value = node.value
            if isinstance(value, ast.Attribute):
                assert value.attr != "READY", "stall.py must never propose a transition to READY; that would clear a halt"

    def test_the_module_transitions_only_as_a_service_actor(self):
        # HUMAN would make the human-only recovery edges reachable from the engine.
        assert stall_module._STALL_ACTOR is ActorKind.SERVICE

        tree = ast.parse(Path(inspect.getfile(stall_module)).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "HUMAN":
                pytest.fail("stall.py must not reference ActorKind.HUMAN; the engine is never a human actor")

    def test_a_service_actor_is_rejected_by_transition_behaviourally(self):
        # The source assertion above proves no such path exists; this proves it
        # would be refused even if one were added.
        result = transition(
            NodeState.HALTED,
            NodeState.READY,
            actor_kind=ActorKind.SERVICE,
            reason="engine attempting to clear its own halt",
        )
        assert result.allowed is False
        assert result.new_state is None
        assert "requires actor_kind" in result.rejection_reason

    def test_a_human_actor_can_clear_a_halt(self):
        # The override exists — it is just human-only. Asserted so this file pins
        # "human-only", not "impossible".
        result = transition(NodeState.HALTED, NodeState.READY, actor_kind=ActorKind.HUMAN, reason="operator override")
        assert result.allowed is True
        assert result.new_state is NodeState.READY

    async def test_a_halted_node_is_never_re_examined_by_the_engine(self, session, sns, notify_config):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", state=NodeState.HALTED, attempts=99, running_for=timedelta(days=30))

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.nodes_examined == 0
        assert await _state_of(session, node.id) == NodeState.HALTED.value


class TestNoDirectStateUpdate:
    """Detection proposes; `transition()` decides. AST-level, same shape as the tick's guard."""

    @staticmethod
    def _stall_ast() -> ast.Module:
        return ast.parse(Path(inspect.getfile(stall_module)).read_text())

    def test_the_module_never_builds_an_update_statement(self):
        # `stall.py` routes every write through `apply_guarded_transition`, so it has
        # no business constructing `update()` at all.
        tree = self._stall_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                assert name != "update", "stall.py must not build UPDATE statements; state writes go through apply_guarded_transition"

    def test_the_module_never_builds_raw_sql(self):
        tree = self._stall_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "text":
                pytest.fail("stall.py must not build raw SQL via text()")

    def test_no_raw_update_statement_in_any_string_literal(self):
        import re

        tree = self._stall_ast()
        docstrings = {
            ast.get_docstring(node) for node in ast.walk(tree) if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        }
        sql_update = re.compile(r"\bupdate\s+\w+\s+set\b", re.IGNORECASE)
        for literal in ast.walk(tree):
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str) and literal.value not in docstrings:
                assert not sql_update.search(literal.value), f"no raw UPDATE statement in a string literal: {literal.value!r}"

    def test_state_writes_go_through_the_tick_s_single_audited_seam(self):
        # Reuse, not a second concurrency discipline. The story is explicit: "do not
        # invent a second one."
        tree = self._stall_ast()
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "tick" for alias in node.names}
        assert "apply_guarded_transition" in imported, "stall.py must reuse the tick's guarded-write seam"

    def test_the_vocabulary_comes_from_the_single_declared_module(self):
        # R-N2a: a local copy of the vocabulary would be a requirement violation.
        tree = self._stall_ast()
        for name in ("NodeState", "TERMINAL_STATES"):
            sources = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and any(a.name == name for a in node.names)}
            assert sources == {"state"}, f"{name} must come from .state, not {sources}"


class TestNotificationFailureIsLoud:
    """R-NF3: detection that told nobody is not a success."""

    async def test_a_failing_notification_forces_non_success(self, session, monkeypatch, notify_config, caplog):
        monkeypatch.setattr(
            "src.orchestration.notify._sns_client",
            lambda _region: _RecordingSNS(fail_with=RuntimeError("SNS unavailable")),
        )
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="a", running_for=PAST_THRESHOLD)

        with caplog.at_level("ERROR"):
            report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.notifications_failed == 1
        assert report.notifications_sent == 0
        assert report.success is False, "an undelivered notification must NOT report success"
        assert any(rec.levelname == "ERROR" for rec in caplog.records)
        # The detection itself still stands — the node really is stalled, and losing
        # that would be worse than the undelivered alert.
        assert report.stalls_detected == 1
        assert await _state_of(session, node.id) == NodeState.FAILED.value

    async def test_an_unconfigured_target_is_a_recorded_error_not_a_silent_skip(self, session, monkeypatch):
        # An environment where nobody wired the topic must be discovered by the
        # first stall, not by the first postmortem.
        monkeypatch.delenv("BG_ORCH_NOTIFY_TOPIC_ARN", raising=False)
        flow = await _make_flow(session)
        await _make_node(session, flow, node_ref="a", running_for=PAST_THRESHOLD)

        report = await detect_stalls(session, NOW, notify_config=NotifyConfig(topic_arn=None))

        assert report.notifications_failed == 1
        assert report.success is False

    async def test_one_undeliverable_notification_does_not_stop_the_others(self, session, monkeypatch, notify_config):
        # Containment, not silence: the pass keeps going AND the failure is visible.
        calls = {"n": 0}
        real_notify = stall_module.notify

        def flaky(notification, config=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise NotificationError("first one fails")
            return real_notify(notification, config)

        client = _RecordingSNS()
        monkeypatch.setattr("src.orchestration.notify._sns_client", lambda _region: client)
        monkeypatch.setattr(stall_module, "notify", flaky)

        flow = await _make_flow(session)
        for ref in ("a", "b", "c"):
            await _make_node(session, flow, node_ref=ref, running_for=PAST_THRESHOLD)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.stalls_detected == 3, "every stalled node is still detected"
        assert report.notifications_failed == 1
        assert report.notifications_sent == 2
        assert report.success is False

    async def test_a_db_read_failure_is_not_an_empty_result(self, session, monkeypatch, caplog):
        # The nastiest silent-stall shape: a read failure that looks like "nothing
        # to do". Mirrors the tick's equivalent test.
        async def boom(*_args, **_kwargs):
            raise RuntimeError("connection reset mid-pass")

        monkeypatch.setattr(stall_module, "_fetch_candidate_page", boom)

        with caplog.at_level("ERROR"):
            report = await detect_stalls(session, NOW)

        assert report.success is False
        assert report.errors == 1
        assert report.nodes_examined == 0

    async def test_a_per_node_failure_is_contained_but_reported(self, session, monkeypatch, notify_config):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("examine blew up")

        monkeypatch.setattr(stall_module, "_examine", boom)

        flow = await _make_flow(session)
        await _make_node(session, flow, node_ref="a", running_for=PAST_THRESHOLD)
        await _make_node(session, flow, node_ref="b", running_for=PAST_THRESHOLD)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.errors == 2
        assert report.success is False

    def test_report_success_accounts_for_delivery_not_only_errors(self):
        report = StallReport()
        assert report.success
        report.record(ORG_A, "notifications_failed")
        assert report.success is False, "delivery failure alone must make the pass unsuccessful"


class TestTenantIsolation:
    """A stall in one org never notifies another."""

    async def test_notifications_carry_their_own_org(self, session, sns, notify_config):
        flow_a = await _make_flow(session, org_id=ORG_A, slug="flow-a")
        flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-b")
        await _make_node(session, flow_a, node_ref="a", running_for=PAST_THRESHOLD)
        await _make_node(session, flow_b, node_ref="b", running_for=PAST_THRESHOLD)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.stalls_detected == 2
        orgs = sorted(p["MessageAttributes"]["org_id"]["StringValue"] for p in sns.publishes)
        assert orgs == [ORG_A, ORG_B]

    async def test_counts_are_broken_out_per_org(self, session, sns, notify_config):
        flow_a = await _make_flow(session, org_id=ORG_A, slug="flow-a")
        flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-b")
        await _make_node(session, flow_a, node_ref="a", running_for=PAST_THRESHOLD)
        await _make_node(session, flow_b, node_ref="b", running_for=PAST_THRESHOLD)
        await _make_node(session, flow_b, node_ref="c", attempts=DEFAULT_DEFECT_CYCLE_BOUND)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.per_org[ORG_A]["stalls_detected"] == 1
        assert report.per_org[ORG_A]["halts_detected"] == 0
        assert report.per_org[ORG_B]["stalls_detected"] == 1
        assert report.per_org[ORG_B]["halts_detected"] == 1

    async def test_a_decision_row_carries_the_node_s_own_org(self, session, sns, notify_config):
        flow = await _make_flow(session, org_id=ORG_A)
        node = await _make_node(session, flow, node_ref="a", running_for=PAST_THRESHOLD, org_id=ORG_A)

        await detect_stalls(session, NOW, notify_config=notify_config)

        decisions = await _decisions(session, node.id)
        assert decisions[0].org_id == ORG_A


class TestBoundedReads:
    """R-NF7: the pass is bounded and paginated, and truncation is not silent."""

    async def test_reads_are_paginated(self, session, sns, notify_config, monkeypatch):
        monkeypatch.setattr(stall_module, "_PAGE_SIZE", 3)
        flow = await _make_flow(session)
        for index in range(7):
            await _make_node(session, flow, node_ref=f"n{index}", running_for=PAST_THRESHOLD)

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.nodes_examined == 7
        assert report.stalls_detected == 7
        assert report.pages_read == 3, "7 rows at a page size of 3 must take 3 pages"
        assert report.truncated is False

    async def test_the_backstop_bounds_the_scan_and_says_so(self, session, sns, notify_config, monkeypatch, caplog):
        monkeypatch.setattr(stall_module, "_PAGE_SIZE", 2)
        monkeypatch.setattr(stall_module, "_ITEM_BACKSTOP", 4)
        flow = await _make_flow(session)
        for index in range(10):
            await _make_node(session, flow, node_ref=f"n{index}", running_for=PAST_THRESHOLD)

        with caplog.at_level("WARNING"):
            report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.nodes_examined == 4, "the backstop must bound the scan"
        assert report.truncated is True, "running out of budget must not read as having nothing left to do"


class TestDefaults:
    """`detect_stalls` is usable with no arguments — that is how the Lambda calls it."""

    async def test_now_defaults_to_the_clock(self, session, sns, monkeypatch):
        monkeypatch.setenv("BG_ORCH_NOTIFY_TOPIC_ARN", TOPIC)
        flow = await _make_flow(session)
        # Entered `running` long before any plausible clock reading.
        node = OrchestrationNode(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref="4191",
            wave_ref="wave-4",
            node_ref="a",
            kind="story",
            title="Node a",
            state=NodeState.RUNNING.value,
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
            updated_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        session.add(node)
        await session.flush()

        report = await detect_stalls(session)

        assert report.stalls_detected == 1
        assert report.notifications_sent == 1

    async def test_a_naive_timestamp_does_not_break_the_arithmetic(self, session, sns, notify_config):
        # SQLite hands back naive datetimes where asyncpg returns aware ones. The
        # elapsed calculation must work on both rather than raising on one.
        flow = await _make_flow(session)
        node = OrchestrationNode(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref="4191",
            wave_ref="wave-4",
            node_ref="a",
            kind="story",
            title="Node a",
            state=NodeState.RUNNING.value,
            created_at=(NOW - PAST_THRESHOLD).replace(tzinfo=None),
            updated_at=(NOW - PAST_THRESHOLD).replace(tzinfo=None),
        )
        session.add(node)
        await session.flush()

        report = await detect_stalls(session, NOW, notify_config=notify_config)

        assert report.stalls_detected == 1
