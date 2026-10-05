"""Focused recovery semantics for the durable execution runner (#5143)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.features import routes as features_routes
from src.orchestration import execution_runner as runner_module
from src.orchestration.execution_policy import Action, ExecutionPolicy, PolicyLimits
from src.orchestration.execution_runner import (
    DecisionKind,
    EffectOutcome,
    EffectRequest,
    EffectResult,
    HandlerDecision,
    HandlerObservation,
    ObservationKind,
    OperationIdentity,
    RunnerConfig,
    RunnerContext,
    run_execution_runner,
    verify_live_authority,
)
from src.orchestration.execution_state import (
    ActionIntent,
    ActionStatus,
    BlockCode,
    BlockRecord,
    ExecutionIdentity,
    ExecutionOutcome,
    ExecutionPhase,
    ExecutionStatus,
    OutcomeKind,
)
from src.orchestration.execution_store import create_execution, prepare_action
from src.orchestration.models import (
    ClaimState,
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.notify import NotificationError
from src.orchestration.work_claims import OwnerKind
from src.shared.models.base import Base

ORG = "org-runner"
CLAIM = "claim-runner"
NOW = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)


class FrozenClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now
        self.ticks = 0.0

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.ticks

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        self.ticks += seconds


class SyntheticHandler:
    """A phase adapter with injected provider facts and I/O."""

    def __init__(
        self,
        *,
        observation: HandlerObservation | None = None,
        decision: HandlerDecision | None = None,
        effect_result: EffectResult | None = None,
        perform_delay: float = 0,
    ) -> None:
        self.observation = observation or HandlerObservation(ObservationKind.READY)
        self.decision = decision or HandlerDecision(DecisionKind.WAIT)
        self.effect_result = effect_result or EffectResult(EffectOutcome.SUCCEEDED, receipt_ref="provider/receipt-1")
        self.perform_delay = perform_delay
        self.observe_count = 0
        self.perform_count = 0

    async def observe(self, context: RunnerContext) -> HandlerObservation:
        self.observe_count += 1
        return self.observation

    def decide(self, context: RunnerContext, observation: HandlerObservation) -> HandlerDecision:
        return self.decision

    async def perform(self, context: RunnerContext, effect: EffectRequest) -> EffectResult:
        self.perform_count += 1
        if self.perform_delay:
            await asyncio.sleep(self.perform_delay)
        return self.effect_result


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _disable_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(eng.sync_engine, "begin")
    def _explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with eng.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _policy() -> dict:
    policy = ExecutionPolicy(
        org_id=ORG,
        repository_ids=["repo-1"],
        allowed_actions=[Action.DEVELOP, Action.REPAIR],
        expires_at=NOW + timedelta(days=7),
        limits=PolicyLimits(
            max_wall_clock_seconds=3600,
            max_spend_usd=Decimal("25"),
            max_attempts_per_node=3,
            max_concurrent_actions=2,
        ),
    )
    return policy.model_dump(mode="json")


@pytest.fixture
async def execution(session_factory):
    async with session_factory() as session:
        flow = OrchestrationFlow(execution_paused=False, org_id=ORG, slug="runner-flow", title="Runner flow")
        session.add(flow)
        await session.flush()
        node = OrchestrationNode(
            org_id=ORG,
            flow_id=flow.id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="N1",
            kind="story",
            state="running",
            title="Runner node",
        )
        session.add(node)
        session.add(
            OrchestrationAcceptedPlan(
                org_id=ORG,
                flow_id=flow.id,
                version=1,
                plan_document={"execution_policy": _policy()},
                plan_hash="a" * 64,
            )
        )
        session.add(
            OrchestrationWorkClaim(
                id=CLAIM,
                org_id=ORG,
                provider_repository_id=123,
                issue_number=5143,
                owner_kind=OwnerKind.ENGINE_FLOW.value,
                owner_ref=flow.id,
                state=ClaimState.HELD.value,
                generation=1,
                active_run_id="runner",
            )
        )
        await session.flush()
        identity = ExecutionIdentity(
            org_id=ORG,
            node_id=node.id,
            cycle=1,
            accepted_plan_version=1,
            claim_id=CLAIM,
            claim_generation=1,
        )
        created = await create_execution(session, identity=identity, flow_id=flow.id, next_check_at=NOW)
        await session.commit()
        return created.record


def _config(**overrides) -> RunnerConfig:
    values = {
        "enabled": True,
        "max_actions": 10,
        "io_timeout_seconds": 1,
        "time_budget_seconds": 30,
        "retry_seconds": 1,
        "max_attempts": 3,
    }
    values.update(overrides)
    return RunnerConfig(**values)


async def _allow(_factory, _record, _effect, _now):
    return None


def _effect_decision(*, operation_key: str = "provider:effect:1", phase: ExecutionPhase = ExecutionPhase.SUBMITTING):
    return HandlerDecision(
        DecisionKind.EFFECT,
        phase=phase,
        effect=EffectRequest(ActionIntent(operation_key=operation_key, kind="synthetic_provider_effect"), Action.DEVELOP),
    )


async def _row_state(session_factory):
    async with session_factory() as session:
        execution = (await session.execute(select(OrchestrationExecution))).scalar_one()
        actions = (await session.execute(select(OrchestrationAction).order_by(OrchestrationAction.operation_key))).scalars().all()
        return execution, actions


class TestFeatureFlagIsFailClosed:
    """The runner's own flag parse must not diverge from the gateway's strict helper.

    This module reads the env var directly rather than importing the helper (which
    lives in a routes module), so the risk is silent divergence: a runner that starts
    on a value the features endpoint reports as *disabled* would perform provider
    effects for an engine the UI says is switched off. Mirrors the pinning already in
    place for `diagnose.py` and `tracker_projection.py`.
    """

    @pytest.fixture(autouse=True)
    def _no_flag_env(self, monkeypatch):
        monkeypatch.delenv(runner_module.FEATURE_FLAG_ENV, raising=False)

    def test_an_absent_flag_resolves_to_disabled(self):
        assert RunnerConfig.from_env().enabled is False

    @pytest.mark.parametrize("value", ["", "False", "0", "no", "off", "1", "yes", "on", "enabled", "TRUE-ish"])
    def test_only_the_literal_true_enables_it(self, monkeypatch, value):
        """`"1"`/`"yes"`/`"on"` are deliberate: truthy-string semantics must not start the runner."""
        monkeypatch.setenv(runner_module.FEATURE_FLAG_ENV, value)
        assert RunnerConfig.from_env().enabled is False

    @pytest.mark.parametrize("value", ["true", "TRUE", "True", "tRuE", " true "])
    def test_explicit_true_enables_it(self, monkeypatch, value):
        monkeypatch.setenv(runner_module.FEATURE_FLAG_ENV, value)
        assert RunnerConfig.from_env().enabled is True

    def test_the_flag_semantics_match_the_gateway_s_strict_helper(self, monkeypatch):
        for value in ["true", "TRUE", "True", "", "false", "0", "1", "yes", "on", "off", "enabled"]:
            monkeypatch.setenv(runner_module.FEATURE_FLAG_ENV, value)
            assert RunnerConfig.from_env().enabled is features_routes._is_enabled_strict(runner_module.FEATURE_FLAG_ENV), f"divergence on {value!r}"


def test_protocol_refuses_incomplete_effect_and_block_decisions():
    with pytest.raises(ValueError, match="EffectRequest"):
        HandlerDecision(DecisionKind.EFFECT)
    with pytest.raises(ValueError, match="BlockRecord"):
        HandlerDecision(DecisionKind.BLOCK)
    with pytest.raises(ValueError, match="BlockRecord"):
        HandlerObservation(ObservationKind.BLOCKED)
    with pytest.raises(ValueError, match="positive"):
        EffectRequest(ActionIntent("effect", "kind"), Action.DEVELOP, timeout_seconds=0)
    with pytest.raises(ValueError, match="block decision"):
        HandlerDecision(DecisionKind.WAIT, status=ExecutionStatus.BLOCKED)
    with pytest.raises(ValueError, match="terminal"):
        HandlerDecision(DecisionKind.ADVANCE, status=ExecutionStatus.CONCLUDED)
    with pytest.raises(ValueError, match="positive"):
        RunnerConfig(enabled=True, max_actions=0)


async def test_operation_identity_binds_authority_cycle_and_provider_head(execution):
    context = RunnerContext(
        identity=ExecutionIdentity(ORG, execution.node_id, execution.cycle, 1, CLAIM, 1),
        execution=execution,
        now=NOW,
    )
    first = OperationIdentity.from_context(context, "merge", "pr=77", "head=abc123").key
    changed = OperationIdentity.from_context(context, "merge", "pr=77", "head=def456").key
    assert first != changed
    assert f"execution={execution.id}" in first
    assert "cycle=1" in first
    assert "plan=1" in first
    assert f"claim={CLAIM}" in first
    assert "head=abc123" in first


async def test_operation_identity_keeps_distinct_binding_tuples_distinct(execution):
    """A ':' inside a binding must not let two different effects share one key.

    The key is the only thing standing between a retry and a duplicate external
    effect, so a shifted delimiter boundary collapsing two tuples together would let
    the second effect adopt the first's prepared action and never be carried out.
    """
    context = RunnerContext(
        identity=ExecutionIdentity(ORG, execution.node_id, execution.cycle, 1, CLAIM, 1),
        execution=execution,
        now=NOW,
    )
    shifted_left = OperationIdentity.from_context(context, "merge", "title=a", "body=b:title=c").key
    shifted_right = OperationIdentity.from_context(context, "merge", "title=a:body=b", "title=c").key
    assert shifted_left != shifted_right

    # Arity alone must register: one joined binding is not the same operation as two.
    assert (
        OperationIdentity.from_context(context, "merge", "pr=77:head=abc").key
        != OperationIdentity.from_context(context, "merge", "pr=77", "head=abc").key
    )

    # The digest namespace must not be forgeable by a literal binding that spells it.
    long_bindings = tuple(f"file={index:03d}-{'x' * 40}" for index in range(12))
    digest_key = OperationIdentity.from_context(context, "merge", *long_bindings).key
    assert "bindings_sha256=" in digest_key
    forged = digest_key.split("bindings_sha256=", 1)[1]
    assert OperationIdentity.from_context(context, "merge", f"bindings_sha256={forged}").key != digest_key


async def test_operation_key_reuse_by_a_different_operation_is_refused(session_factory, execution):
    """A key held by another kind of action must refuse, not hand back its receipt."""
    identity = ExecutionIdentity(ORG, execution.node_id, execution.cycle, 1, CLAIM, 1)
    async with session_factory() as session:
        first = await prepare_action(
            session,
            identity=identity,
            intent=ActionIntent("shared-key", "merge_pull_request"),
        )
        assert first.kind is OutcomeKind.APPLIED
        await session.commit()

    async with session_factory() as session:
        clash = await prepare_action(
            session,
            identity=identity,
            intent=ActionIntent("shared-key", "deploy_environment"),
        )
        assert clash.kind is OutcomeKind.CONFLICT
        assert "already held by a merge_pull_request action" in (clash.reason or "")
        # The original stands; no second action was invented under the same key.
        assert clash.action is None

    async with session_factory() as session:
        actions = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert [action.kind for action in actions] == ["merge_pull_request"]

    # The genuine retry path is untouched: the same operation still adopts the original.
    async with session_factory() as session:
        retry = await prepare_action(
            session,
            identity=identity,
            intent=ActionIntent("shared-key", "merge_pull_request"),
        )
        assert retry.kind is OutcomeKind.APPLIED
        assert retry.reason == "action_already_prepared"


async def test_intent_is_committed_before_provider_io(session_factory, execution):
    handler = SyntheticHandler(decision=_effect_decision())
    observed: dict[str, object] = {}

    async def checkpoint(name, _context):
        if name != "after_intent":
            return
        row, actions = await _row_state(session_factory)
        observed["status"] = row.status
        observed["action"] = actions[0].status
        observed["perform_count"] = handler.perform_count

    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=_allow,
        checkpoint=checkpoint,
    )

    assert report.effects_succeeded == 1
    assert observed == {
        "status": ExecutionStatus.AWAITING_EXTERNAL.value,
        "action": ActionStatus.PREPARED.value,
        "perform_count": 0,
    }
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.RUNNABLE.value
    assert row.phase == ExecutionPhase.SUBMITTING.value
    assert actions[0].status == ActionStatus.SUCCEEDED.value
    assert actions[0].receipt_ref == "provider/receipt-1"


async def test_process_death_after_intent_recovers_by_observing_not_repeating(session_factory, execution):
    first = SyntheticHandler(decision=_effect_decision())

    async def die_after_intent(name, _context):
        if name == "after_intent":
            raise RuntimeError("simulated process death")

    clock = FrozenClock()
    failed = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: first},
        config=_config(),
        clock=clock,
        authority_verifier=_allow,
        checkpoint=die_after_intent,
    )
    assert failed.errors == 1
    assert first.perform_count == 0
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.AWAITING_EXTERNAL.value
    assert actions[0].status == ActionStatus.PREPARED.value

    clock.advance(2)
    recovering = SyntheticHandler(
        observation=HandlerObservation(
            ObservationKind.UNCERTAIN,
            operation_key="provider:effect:1",
            detail="provider could not prove the outcome",
        ),
        # Even a buggy adapter asking to repeat is fenced by the runner.
        decision=_effect_decision(operation_key="provider:effect:2"),
    )
    recovered = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: recovering},
        config=_config(),
        clock=clock,
        authority_verifier=_allow,
    )
    assert recovered.effects_uncertain == 1
    assert recovering.perform_count == 0
    row, actions = await _row_state(session_factory)
    assert len(actions) == 1
    assert actions[0].status == ActionStatus.UNKNOWN.value
    assert row.status == ExecutionStatus.AWAITING_EXTERNAL.value
    assert row.next_check_at is not None


async def test_process_death_before_intent_leaves_original_work_due(session_factory, execution):
    handler = SyntheticHandler(decision=_effect_decision())

    async def die_while_observing(_context):
        raise RuntimeError("simulated death before intent")

    handler.observe = die_while_observing
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=_allow,
    )
    assert report.errors == 1
    row, actions = await _row_state(session_factory)
    assert actions == []
    assert row.status == ExecutionStatus.RUNNABLE.value
    assert row.next_check_at is not None
    assert row.next_check_at.replace(tzinfo=UTC) <= NOW


async def test_death_after_remote_success_reconciles_receipt_before_any_retry(session_factory, execution):
    first = SyntheticHandler(decision=_effect_decision())

    async def die_after_effect(name, _context):
        if name == "after_effect":
            raise RuntimeError("simulated death after provider success")

    clock = FrozenClock()
    failed = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: first},
        config=_config(),
        clock=clock,
        authority_verifier=_allow,
        checkpoint=die_after_effect,
    )
    assert failed.errors == 1
    assert first.perform_count == 1
    row, actions = await _row_state(session_factory)
    assert actions[0].status == ActionStatus.PREPARED.value

    clock.advance(2)
    recovering = SyntheticHandler(
        observation=HandlerObservation(
            ObservationKind.SUCCEEDED,
            operation_key="provider:effect:1",
            receipt_ref="provider/recovered-receipt",
        ),
        decision=HandlerDecision(DecisionKind.CONCLUDE),
    )
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: recovering},
        config=_config(),
        clock=clock,
        authority_verifier=_allow,
    )
    assert report.advanced == 1
    assert recovering.perform_count == 0
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.CONCLUDED.value
    assert actions[0].status == ActionStatus.SUCCEEDED.value
    assert actions[0].receipt_ref == "provider/recovered-receipt"


async def test_effect_timeout_stays_unknown_and_due(session_factory, execution):
    handler = SyntheticHandler(decision=_effect_decision(), perform_delay=0.1)
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(io_timeout_seconds=0.01),
        clock=FrozenClock(),
        authority_verifier=_allow,
    )
    assert report.effects_uncertain == 1
    assert report.effects_succeeded == 0
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.AWAITING_EXTERNAL.value
    assert actions[0].status == ActionStatus.UNKNOWN.value


async def test_already_done_observation_concludes_without_perform(session_factory, execution):
    handler = SyntheticHandler(
        observation=HandlerObservation(ObservationKind.SUCCEEDED, receipt_ref="workflow/existing"),
        decision=HandlerDecision(DecisionKind.ALREADY_DONE, progress_note="matching workflow already completed"),
    )
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=_allow,
    )
    assert report.advanced == 1
    assert handler.perform_count == 0
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.CONCLUDED.value
    assert actions == []


async def test_receipt_and_transactional_settlement_roll_back_together(session_factory, execution):
    async def broken_settlement(session, context):
        await session.execute(update(OrchestrationNode).where(OrchestrationNode.id == context.execution.node_id).values(state="passed"))
        raise RuntimeError("simulated dependency-release failure")

    handler = SyntheticHandler(
        decision=_effect_decision(),
        effect_result=EffectResult(
            EffectOutcome.SUCCEEDED,
            receipt_ref="provider/receipt-before-rollback",
            settlement=broken_settlement,
        ),
    )
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=_allow,
    )
    assert report.errors == 1
    assert report.effects_succeeded == 0
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.AWAITING_EXTERNAL.value
    assert actions[0].status == ActionStatus.PREPARED.value
    async with session_factory() as session:
        node = await session.get(OrchestrationNode, row.node_id)
        assert node.state == "running"


@pytest.mark.parametrize(
    ("observation", "decision", "expected_status"),
    [
        pytest.param(
            HandlerObservation(
                ObservationKind.BLOCKED,
                block=BlockRecord(BlockCode.CREDENTIAL_UNAVAILABLE, "user", "configure a scoped credential"),
            ),
            HandlerDecision(
                DecisionKind.BLOCK,
                block=BlockRecord(BlockCode.CREDENTIAL_UNAVAILABLE, "user", "configure a scoped credential"),
            ),
            ExecutionStatus.BLOCKED,
            id="ready-but-undispatchable",
        ),
        pytest.param(
            HandlerObservation(ObservationKind.WAITING, detail="checks pending"),
            HandlerDecision(DecisionKind.WAIT, phase=ExecutionPhase.AWAITING_REVIEW),
            ExecutionStatus.AWAITING_EXTERNAL,
            id="pull-request-ci",
        ),
        pytest.param(
            HandlerObservation(ObservationKind.WAITING, detail="deployment workflow running"),
            HandlerDecision(DecisionKind.WAIT, phase=ExecutionPhase.SUBMITTING),
            ExecutionStatus.AWAITING_EXTERNAL,
            id="deployment",
        ),
        pytest.param(
            HandlerObservation(ObservationKind.UNCERTAIN, detail="mandatory evaluation still running"),
            HandlerDecision(DecisionKind.WAIT, phase=ExecutionPhase.SETTLING),
            ExecutionStatus.AWAITING_EXTERNAL,
            id="evaluation",
        ),
    ],
)
async def test_wait_classes_keep_a_durable_wakeup(session_factory, execution, observation, decision, expected_status):
    handler = SyntheticHandler(observation=observation, decision=decision)
    await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=_allow,
        notifier=lambda _notification: "sns/wait-class",
    )
    row, _ = await _row_state(session_factory)
    assert row.status == expected_status.value
    assert row.next_check_at is not None


@pytest.mark.parametrize(
    "code",
    [BlockCode.OWNERSHIP_LOST, BlockCode.BUDGET_EXHAUSTED, BlockCode.HUMAN_REFUSED],
)
async def test_adverse_paths_are_blocked_and_not_completed(session_factory, execution, code):
    block = BlockRecord(code, "operator", "resolve the recorded refusal")
    handler = SyntheticHandler(
        observation=HandlerObservation(ObservationKind.BLOCKED, block=block),
        decision=HandlerDecision(DecisionKind.BLOCK, block=block),
    )
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=_allow,
        notifier=lambda _notification: "sns/message-1",
    )
    assert report.blocked == 1
    assert report.notifications_sent == 1
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == code.value
    assert row.next_check_at is not None
    assert row.notification_receipt_ref == "sns/message-1"
    assert actions[0].status == ActionStatus.SUCCEEDED.value


async def test_notification_failure_preserves_block_and_due_retry(session_factory, execution):
    block = BlockRecord(BlockCode.BUDGET_EXHAUSTED, "operator", "increase or end the bounded budget")
    handler = SyntheticHandler(decision=HandlerDecision(DecisionKind.BLOCK, block=block))

    def fail_notification(_notification):
        raise NotificationError("SNS unavailable")

    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=_allow,
        notifier=fail_notification,
    )
    assert report.notifications_failed == 1
    assert not report.success
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.next_check_at is not None
    assert actions[0].status == ActionStatus.FAILED.value


async def test_successful_block_notification_is_not_sent_again_on_later_ticks(session_factory, execution):
    block = BlockRecord(BlockCode.HUMAN_REFUSED, "requester", "choose an authorized recovery")
    handler = SyntheticHandler(decision=HandlerDecision(DecisionKind.BLOCK, block=block))
    sends = 0

    def counted_notification(_notification):
        nonlocal sends
        sends += 1
        return "sns/only-message"

    clock = FrozenClock()
    first = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=clock,
        authority_verifier=_allow,
        notifier=counted_notification,
    )
    assert first.notifications_sent == 1
    clock.advance(2)
    await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=clock,
        authority_verifier=_allow,
        notifier=counted_notification,
    )
    assert sends == 1
    _, actions = await _row_state(session_factory)
    assert len(actions) == 1


async def test_notification_retries_are_bounded_and_exhaustion_is_visible(session_factory, execution):
    block = BlockRecord(BlockCode.BUDGET_EXHAUSTED, "operator", "increase or end the bounded budget")
    handler = SyntheticHandler(decision=HandlerDecision(DecisionKind.BLOCK, block=block))
    sends = 0

    def always_fail(_notification):
        nonlocal sends
        sends += 1
        raise NotificationError("SNS unavailable")

    clock = FrozenClock()
    for _ in range(3):
        await run_execution_runner(
            session_factory,
            handlers={ExecutionPhase.ADMITTED: handler},
            config=_config(max_attempts=2),
            clock=clock,
            authority_verifier=_allow,
            notifier=always_fail,
        )
        clock.advance(2)

    assert sends == 2
    row, actions = await _row_state(session_factory)
    assert len(actions) == 2
    assert all(action.status == ActionStatus.FAILED.value for action in actions)
    assert row.progress_note == "block notification attempts exhausted (2/2)"
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.next_check_at is not None


async def test_undeliverable_block_notice_stops_failing_every_later_tick(session_factory, execution):
    """An exhausted notice keeps the block due without permanently failing the tick.

    The delivery failures themselves must fail their own passes. Once retries are
    spent the condition is durable and no retry clears it, so continuing to report a
    fresh failure would leave the scheduled tick red forever and mask a real new
    failure in an unrelated pass.
    """
    block = BlockRecord(BlockCode.BUDGET_EXHAUSTED, "operator", "increase or end the bounded budget")
    handler = SyntheticHandler(decision=HandlerDecision(DecisionKind.BLOCK, block=block))

    def always_fail(_notification):
        raise NotificationError("SNS unavailable")

    clock = FrozenClock()
    passes = []
    for _ in range(5):
        report = await run_execution_runner(
            session_factory,
            handlers={ExecutionPhase.ADMITTED: handler},
            config=_config(max_attempts=2),
            clock=clock,
            authority_verifier=_allow,
            notifier=always_fail,
        )
        passes.append(report)
        clock.advance(2)

    # The two real delivery attempts fail their own passes.
    assert [report.notifications_failed for report in passes[:2]] == [1, 1]
    assert not passes[0].success and not passes[1].success
    # Afterwards the exhaustion is reported as unresolved, not as a new failure.
    assert all(report.notifications_failed == 0 for report in passes[2:])
    assert all(report.notifications_unresolved == 1 for report in passes[2:])
    assert all(report.success for report in passes[2:])

    row, actions = await _row_state(session_factory)
    assert len(actions) == 2
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == BlockCode.BUDGET_EXHAUSTED.value
    assert row.next_check_at is not None


async def test_uncertain_block_notice_is_not_resent_and_does_not_fail_later_ticks(session_factory, execution):
    """A delivery whose outcome is unknown stays unknown, visible and non-duplicating."""
    block = BlockRecord(BlockCode.HUMAN_REFUSED, "requester", "choose an authorized recovery")
    handler = SyntheticHandler(decision=HandlerDecision(DecisionKind.BLOCK, block=block))
    sends = 0

    async def times_out(_notification):
        await asyncio.sleep(5)
        return "sns/never-known"

    def would_send(_notification):
        nonlocal sends
        sends += 1
        return "sns/second-attempt"

    clock = FrozenClock()
    first = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=clock,
        authority_verifier=_allow,
        notifier=times_out,
    )
    assert first.notifications_failed == 1
    assert not first.success

    later = []
    for _ in range(3):
        clock.advance(2)
        later.append(
            await run_execution_runner(
                session_factory,
                handlers={ExecutionPhase.ADMITTED: handler},
                config=_config(),
                clock=clock,
                authority_verifier=_allow,
                notifier=would_send,
            )
        )

    # An uncertain send is never repeated, and its durable uncertainty is not a new failure.
    assert sends == 0
    assert all(report.notifications_unresolved == 1 for report in later)
    assert all(report.notifications_failed == 0 for report in later)
    assert all(report.success for report in later)

    row, actions = await _row_state(session_factory)
    assert [action.status for action in actions] == [ActionStatus.UNKNOWN.value]
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.next_check_at is not None


class ScriptedHandler:
    """A phase adapter whose decision changes from tick to tick."""

    def __init__(self, decisions, *, observation: HandlerObservation | None = None) -> None:
        self.decisions = list(decisions)
        self.calls = -1
        self.observation = observation or HandlerObservation(ObservationKind.READY)
        self.perform_count = 0

    async def observe(self, context: RunnerContext) -> HandlerObservation:
        return self.observation

    def decide(self, context: RunnerContext, observation: HandlerObservation) -> HandlerDecision:
        self.calls += 1
        return self.decisions[min(self.calls, len(self.decisions) - 1)]

    async def perform(self, context: RunnerContext, effect: EffectRequest) -> EffectResult:
        self.perform_count += 1
        return EffectResult(EffectOutcome.SUCCEEDED, receipt_ref="provider/scripted")


async def test_recurring_block_is_persisted_again_after_an_uncertain_notice(session_factory, execution):
    """A block that cleared and recurred must be durable, even if its notice is suppressed.

    The action row proves a notification was attempted for this block code; it does not
    prove the *current* block is recorded. Clearing an execution NULLs every block
    column, so a later recurrence under the same code finds a stale `unknown` action
    row and suppresses the resend. Suppressing the send must not suppress the write:
    otherwise the tick reports the execution as blocked while the row still reads
    `runnable` with no block code, no owner and no required_input — a live block that
    the read model and every operator view report as healthy running work.
    """
    handler = ScriptedHandler(
        [
            HandlerDecision(
                DecisionKind.BLOCK,
                block=BlockRecord(BlockCode.AUTHORITY_UNVERIFIABLE, "platform-operator", "first occurrence"),
            ),
            HandlerDecision(DecisionKind.ADVANCE, phase=ExecutionPhase.SUBMITTING),
            HandlerDecision(
                DecisionKind.BLOCK,
                block=BlockRecord(BlockCode.AUTHORITY_UNVERIFIABLE, "platform-operator", "second occurrence"),
            ),
        ]
    )

    async def times_out(_notification):
        await asyncio.sleep(5)
        return "sns/never-known"

    clock = FrozenClock()
    reports = []
    for _ in range(3):
        reports.append(
            await run_execution_runner(
                session_factory,
                handlers={ExecutionPhase.ADMITTED: handler, ExecutionPhase.SUBMITTING: handler},
                config=_config(),
                clock=clock,
                authority_verifier=_allow,
                notifier=times_out,
            )
        )
        clock.advance(5)

    # The uncertain notice is still not resent: exactly one notify action exists.
    row, actions = await _row_state(session_factory)
    notices = [action for action in actions if action.kind == "notify_execution_block"]
    assert [action.status for action in notices] == [ActionStatus.UNKNOWN.value]
    assert reports[2].notifications_unresolved == 1

    # ...but the recurrence is durable, and carries the *current* instructions.
    assert reports[2].blocked == 1
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == BlockCode.AUTHORITY_UNVERIFIABLE.value
    assert row.block_owner == "platform-operator"
    assert row.block_required_input == "second occurrence"
    assert row.next_check_at is not None


async def test_recurring_block_is_persisted_again_after_notice_retries_are_exhausted(session_factory, execution):
    """The exhausted-notice path must key on the durable block, not on the progress note.

    `advance_execution` only writes a non-None progress note, so the exhaustion note
    survives an intervening clear. Using it as the "already recorded" proxy therefore
    skips the write for a genuinely new block occurrence and leaves the same
    blocked-but-reads-runnable row as the uncertain path.
    """
    budget_block = HandlerDecision(
        DecisionKind.BLOCK,
        block=BlockRecord(BlockCode.BUDGET_EXHAUSTED, "operator", "raise the bounded budget"),
    )
    # Two failed deliveries, then a pass that takes the exhausted branch and writes the
    # note, then a clear, then the recurrence. The exhaustion note must already be on
    # the row when the block recurs — that is the state in which the note is no longer
    # evidence that this block is durable.
    handler = ScriptedHandler(
        [
            budget_block,
            budget_block,
            budget_block,
            HandlerDecision(DecisionKind.ADVANCE, phase=ExecutionPhase.SUBMITTING),
            budget_block,
        ]
    )

    def always_fail(_notification):
        raise NotificationError("SNS unavailable")

    clock = FrozenClock()
    reports = []
    for _ in range(5):
        reports.append(
            await run_execution_runner(
                session_factory,
                handlers={ExecutionPhase.ADMITTED: handler, ExecutionPhase.SUBMITTING: handler},
                config=_config(max_attempts=2),
                clock=clock,
                authority_verifier=_allow,
                notifier=always_fail,
            )
        )
        clock.advance(2)

    # Retries stay bounded: two failed deliveries, then exhaustion, and no third send.
    row, actions = await _row_state(session_factory)
    assert len([action for action in actions if action.kind == "notify_execution_block"]) == 2
    assert reports[4].notifications_unresolved == 1

    # The recurrence after the clear is durable rather than silently dropped.
    assert reports[4].blocked == 1
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == BlockCode.BUDGET_EXHAUSTED.value
    assert row.block_required_input == "raise the bounded budget"
    assert row.next_check_at is not None


async def test_blocking_an_execution_keeps_the_unsettled_provider_pointer(session_factory, execution):
    """A block pauses work; it must not erase the pointer to an unsettled provider call.

    `pending_action_key` is the only thing that sends a recovering process to ask the
    provider what happened, and reconciliation is gated on it. An effect whose outcome
    is UNCERTAIN stays `unknown` on the ledger, so dropping the key while blocking
    would strand that action forever: the block clears, no pass ever observes the call
    again, and a provider effect that may well have landed is never settled.
    """
    handler = ScriptedHandler(
        [
            _effect_decision(operation_key="provider:pending-effect"),
            HandlerDecision(
                DecisionKind.BLOCK,
                block=BlockRecord(BlockCode.HUMAN_GATE_REQUIRED, "requester", "approve the pending step"),
            ),
        ],
        observation=HandlerObservation(ObservationKind.WAITING),
    )
    handler.perform = lambda context, effect: _uncertain_effect()  # type: ignore[method-assign]

    clock = FrozenClock()
    await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler, ExecutionPhase.SUBMITTING: handler},
        config=_config(),
        clock=clock,
        authority_verifier=_allow,
        notifier=lambda _notification: "sns/ok",
    )
    row, _ = await _row_state(session_factory)
    assert row.status == ExecutionStatus.AWAITING_EXTERNAL.value
    assert row.pending_action_key == "provider:pending-effect"

    clock.advance(5)
    await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler, ExecutionPhase.SUBMITTING: handler},
        config=_config(),
        clock=clock,
        authority_verifier=_allow,
        notifier=lambda _notification: "sns/ok",
    )

    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == BlockCode.HUMAN_GATE_REQUIRED.value
    # The unsettled effect is still named, so a later pass can reconcile it.
    effect = next(action for action in actions if action.kind == "synthetic_provider_effect")
    assert effect.status == ActionStatus.UNKNOWN.value
    assert row.pending_action_key == "provider:pending-effect"


async def _uncertain_effect() -> EffectResult:
    return EffectResult(EffectOutcome.UNCERTAIN, detail="provider outcome unknown")


async def test_lapsed_deadline_does_not_discard_an_observed_conclusion(session_factory, execution):
    """Observed completion is recorded truth; a lapsed deadline must not bury it.

    The deadline stops work still to be *carried out*. An execution whose effect
    already succeeded remotely would otherwise be stranded as permanently blocked
    while the provider side is done — the ledger would contradict reality.
    """
    async with session_factory() as session:
        await session.execute(
            update(OrchestrationExecution).where(OrchestrationExecution.id == execution.id).values(deadline_at=NOW - timedelta(hours=1))
        )
        await session.commit()

    handler = SyntheticHandler(
        observation=HandlerObservation(ObservationKind.SUCCEEDED, receipt_ref="provider/already-done"),
        decision=HandlerDecision(DecisionKind.ALREADY_DONE, progress_note="provider reports the work complete"),
    )
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=_allow,
        notifier=lambda _notification: "sns/unexpected",
    )

    assert report.advanced == 1
    assert report.blocked == 0
    assert report.notifications_sent == 0
    row, _ = await _row_state(session_factory)
    assert row.status == ExecutionStatus.CONCLUDED.value
    assert row.block_code is None
    assert row.next_check_at is None


async def test_lapsed_deadline_keeps_the_handler_s_own_typed_block(session_factory, execution):
    """A specific block names the party who can clear it; the deadline must not replace it."""
    async with session_factory() as session:
        await session.execute(
            update(OrchestrationExecution).where(OrchestrationExecution.id == execution.id).values(deadline_at=NOW - timedelta(hours=1))
        )
        await session.commit()

    handler = SyntheticHandler(
        decision=HandlerDecision(
            DecisionKind.BLOCK,
            block=BlockRecord(BlockCode.HUMAN_INPUT_REQUIRED, "requester", "answer the outstanding question"),
        )
    )
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=_allow,
        notifier=lambda _notification: "sns/human-input",
    )

    assert report.blocked == 1
    row, _ = await _row_state(session_factory)
    assert row.block_code == BlockCode.HUMAN_INPUT_REQUIRED.value
    assert row.block_owner == "requester"
    assert row.block_required_input == "answer the outstanding question"
    assert row.next_check_at is not None


async def test_lapsed_deadline_still_blocks_work_that_would_be_carried_out(session_factory, execution):
    """The deadline's own purpose is preserved: no new effect is attempted after it."""
    async with session_factory() as session:
        await session.execute(
            update(OrchestrationExecution).where(OrchestrationExecution.id == execution.id).values(deadline_at=NOW - timedelta(hours=1))
        )
        await session.commit()

    handler = SyntheticHandler(decision=_effect_decision())
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=_allow,
        notifier=lambda _notification: "sns/deadline",
    )

    assert report.blocked == 1
    assert handler.perform_count == 0
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == BlockCode.ATTEMPTS_EXHAUSTED.value
    # No provider effect was prepared; only the block notice exists.
    assert {action.kind for action in actions} == {"notify_execution_block"}
    assert row.next_check_at is not None


async def test_blocked_paths_are_attributed_per_org(session_factory, execution):
    """Per-org counters must agree with the totals, or a tenant's blocks read as zero."""
    handler = SyntheticHandler(decision=_effect_decision())

    async def deny(_factory, _record, _effect, _now):
        return BlockRecord(BlockCode.OWNERSHIP_LOST, "orchestration-owner", "reconcile the claim")

    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=deny,
        notifier=lambda _notification: "sns/ownership",
    )

    assert report.blocked == 1
    assert report.per_org[ORG]["blocked"] == 1


async def test_effect_authority_is_rechecked_against_live_policy_and_claim(session_factory, execution):
    permitted = EffectRequest(ActionIntent("effect:develop", "develop"), Action.DEVELOP)
    assert await verify_live_authority(session_factory, execution, permitted, NOW) is None

    denied = await verify_live_authority(
        session_factory,
        execution,
        EffectRequest(ActionIntent("effect:merge", "merge"), Action.MERGE),
        NOW,
    )
    assert denied is not None
    assert denied.code is BlockCode.AUTHORITY_UNVERIFIABLE

    async with session_factory() as session:
        await seed_stage_attempts(session, execution, Action.DEVELOP, 3)
        await session.commit()
    exhausted = await verify_live_authority(session_factory, execution, permitted, NOW)
    assert exhausted is not None
    assert exhausted.code is BlockCode.ATTEMPTS_EXHAUSTED

    async with session_factory() as session:
        claim = await session.get(OrchestrationWorkClaim, CLAIM)
        claim.state = ClaimState.RELEASED.value
        await session.commit()
    withdrawn = await verify_live_authority(session_factory, execution, EffectRequest(ActionIntent("repair:first", "repair"), Action.REPAIR), NOW)
    assert withdrawn is not None
    assert withdrawn.code is BlockCode.OWNERSHIP_LOST


@pytest.mark.parametrize(
    ("owner_kind", "owner_ref"),
    [
        (OwnerKind.DIRECT_DISPATCH.value, None),
        (OwnerKind.ENGINE_FLOW.value, "another-flow"),
    ],
)
async def test_effect_authority_requires_claim_owned_by_execution_flow(session_factory, execution, owner_kind, owner_ref):
    async with session_factory() as session:
        claim = await session.get(OrchestrationWorkClaim, CLAIM)
        claim.owner_kind = owner_kind
        claim.owner_ref = owner_ref or execution.flow_id
        await session.commit()

    block = await verify_live_authority(
        session_factory,
        execution,
        EffectRequest(ActionIntent("effect:develop", "develop"), Action.DEVELOP),
        NOW,
    )

    assert block is not None
    assert block.code is BlockCode.OWNERSHIP_LOST


async def test_authority_denied_before_intent_creates_no_action_row(session_factory, execution):
    handler = SyntheticHandler(decision=_effect_decision())
    checks = 0

    async def deny_before_intent(_factory, record, _effect, _now):
        nonlocal checks
        checks += 1
        return BlockRecord(
            BlockCode.OWNERSHIP_LOST,
            "orchestration-owner",
            "reconcile the current claim",
            progressed_at=record.progressed_at,
        )

    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=deny_before_intent,
        notifier=lambda _notification: "sns/authority-block",
    )

    assert checks == 1
    assert handler.perform_count == 0
    assert report.blocked == 1
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.BLOCKED.value
    assert {action.kind for action in actions} == {"notify_execution_block"}


async def test_authority_withdrawn_after_intent_never_reaches_perform(session_factory, execution):
    handler = SyntheticHandler(decision=_effect_decision())
    checks = 0

    async def withdraw_on_pre_effect(_factory, record, _effect, _now):
        nonlocal checks
        checks += 1
        if checks == 1:
            return None
        return BlockRecord(
            BlockCode.OWNERSHIP_LOST,
            "orchestration-owner",
            "reconcile the current claim",
            progressed_at=record.progressed_at,
        )

    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=withdraw_on_pre_effect,
        notifier=lambda _notification: "sns/authority-block",
    )
    assert checks == 2
    assert handler.perform_count == 0
    assert report.blocked == 1
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.BLOCKED.value
    assert {action.kind for action in actions} == {"synthetic_provider_effect", "notify_execution_block"}
    effect = next(action for action in actions if action.kind == "synthetic_provider_effect")
    assert effect.status == ActionStatus.PREPARED.value


async def test_store_conflict_is_terminal_for_the_pass_and_never_retried(session_factory, execution, monkeypatch):
    handler = SyntheticHandler(decision=_effect_decision())
    calls = 0

    async def conflict(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return ExecutionOutcome(kind=OutcomeKind.CONFLICT, reason="claim_generation_superseded")

    monkeypatch.setattr(runner_module, "advance_execution", conflict)
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=FrozenClock(),
        authority_verifier=_allow,
    )
    assert calls == 1
    assert report.conflicts == 1
    assert handler.perform_count == 0
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.RUNNABLE.value
    assert actions == []


async def test_action_cap_leaves_remaining_execution_due(session_factory, execution):
    async with session_factory() as session:
        first = (await session.execute(select(OrchestrationExecution))).scalar_one()
        first_node = await session.get(OrchestrationNode, first.node_id)
        second_node = OrchestrationNode(
            org_id=ORG,
            flow_id=first.flow_id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="N2",
            kind="story",
            state="running",
            title="Second runner node",
        )
        session.add(second_node)
        await session.flush()
        await create_execution(
            session,
            identity=ExecutionIdentity(ORG, second_node.id, 1, 1, CLAIM, 1),
            flow_id=first.flow_id,
            next_check_at=NOW,
        )
        assert first_node is not None
        await session.commit()

    handler = SyntheticHandler(decision=HandlerDecision(DecisionKind.WAIT))
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(max_actions=1),
        clock=FrozenClock(),
        authority_verifier=_allow,
    )
    assert report.capped
    assert report.examined == 1
    async with session_factory() as session:
        due = (
            await session.execute(select(func.count()).select_from(OrchestrationExecution).where(OrchestrationExecution.next_check_at <= NOW))
        ).scalar_one()
    assert due == 1


async def test_accepted_attempt_allowance_is_spent_on_effects_not_on_the_reservation(session_factory, execution):
    """An accepted allowance of N must perform N effects, not N-1.

    The runner consumes an attempt when it reserves the intent, then re-checks
    authority immediately before the provider call. If that re-check counts the
    just-reserved attempt as already spent, the last granted attempt is retired
    without ever being used: an allowance of 1 performs nothing at all, and the
    reserved intent is stranded as a `prepared` action that no observation settles,
    so the ledger permanently claims an effect might have happened when it provably
    did not.
    """
    policy = ExecutionPolicy(
        org_id=ORG,
        repository_ids=["repo-1"],
        allowed_actions=[Action.DEVELOP, Action.REPAIR],
        expires_at=NOW + timedelta(days=7),
        limits=PolicyLimits(
            max_wall_clock_seconds=3600,
            max_spend_usd=Decimal("25"),
            max_attempts_per_node=1,
            max_concurrent_actions=2,
        ),
    )
    async with session_factory() as session:
        plan = (await session.execute(select(OrchestrationAcceptedPlan))).scalar_one()
        plan.plan_document = {"execution_policy": policy.model_dump(mode="json")}
        await session.commit()

    handler = SyntheticHandler(
        decision=_effect_decision(operation_key="provider:only-allowed-attempt"),
        effect_result=EffectResult(EffectOutcome.FAILED, detail="provider refused"),
    )
    clock = FrozenClock()

    first = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(max_attempts=99),  # the accepted policy, not the config, is the bound under test
        clock=clock,
        authority_verifier=verify_live_authority,
        notifier=lambda _notification: "sns/attempts",
    )

    # The single granted attempt was actually spent on the provider call.
    assert handler.perform_count == 1
    assert first.effects_failed == 1
    row, actions = await _row_state(session_factory)
    assert row.attempts == 1
    # The reserved intent was settled by an observation; nothing is left dangling.
    effect = next(action for action in actions if action.kind == "synthetic_provider_effect")
    assert effect.status == ActionStatus.FAILED.value

    # And the allowance is still a real bound: the next tick blocks instead of retrying.
    clock.advance(5)
    second = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(max_attempts=99),
        clock=clock,
        authority_verifier=verify_live_authority,
        notifier=lambda _notification: "sns/attempts",
    )
    assert handler.perform_count == 1
    assert second.blocked == 1
    row, _ = await _row_state(session_factory)
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == BlockCode.ATTEMPTS_EXHAUSTED.value
    assert row.next_check_at is not None


async def test_unregistered_older_phase_cannot_starve_a_registered_phase(session_factory, execution):
    async with session_factory() as session:
        first = (await session.execute(select(OrchestrationExecution))).scalar_one()
        first.phase = ExecutionPhase.PREPARING.value
        first.next_check_at = NOW - timedelta(hours=1)
        second_node = OrchestrationNode(
            org_id=ORG,
            flow_id=first.flow_id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="N3",
            kind="story",
            state="running",
            title="Registered phase node",
        )
        session.add(second_node)
        await session.flush()
        second = await create_execution(
            session,
            identity=ExecutionIdentity(ORG, second_node.id, 1, 1, CLAIM, 1),
            flow_id=first.flow_id,
            next_check_at=NOW,
        )
        await session.commit()

    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: SyntheticHandler(decision=HandlerDecision(DecisionKind.WAIT))},
        config=_config(max_actions=1),
        clock=FrozenClock(),
        authority_verifier=_allow,
    )
    assert report.examined == 1
    async with session_factory() as session:
        rows = {row.id: row for row in (await session.execute(select(OrchestrationExecution))).scalars()}
    assert rows[first.id].next_check_at.replace(tzinfo=UTC) < NOW
    assert rows[second.record.id].next_check_at.replace(tzinfo=UTC) > NOW


async def test_recurring_block_is_persisted_again_after_a_delivered_notice(session_factory, execution):
    """A block that cleared and recurred must be durable when its notice was DELIVERED.

    The delivered-notice path is the ordinary steady state of a blocked execution, and
    it suppressed its write on the strength of the successful action row alone. That
    row proves a notice was once accepted for this block code; it does not prove the
    block being recorded now is on the row. Clearing an execution NULLs every block
    column, so after a clear and a recurrence the tick reported the execution as
    blocked while the row still read `runnable` with no code, owner or required_input —
    the state an operator cannot act on, and the same defect already fixed on the
    uncertain and exhausted paths.
    """
    handler = ScriptedHandler(
        [
            HandlerDecision(
                DecisionKind.BLOCK,
                block=BlockRecord(BlockCode.HUMAN_REFUSED, "requester", "first occurrence"),
            ),
            HandlerDecision(DecisionKind.ADVANCE, phase=ExecutionPhase.SUBMITTING),
            HandlerDecision(
                DecisionKind.BLOCK,
                block=BlockRecord(BlockCode.HUMAN_REFUSED, "requester", "second occurrence"),
            ),
        ]
    )
    sends = 0

    def delivered(_notification):
        nonlocal sends
        sends += 1
        return "sns/delivered-once"

    clock = FrozenClock()
    reports = []
    for _ in range(3):
        reports.append(
            await run_execution_runner(
                session_factory,
                handlers={ExecutionPhase.ADMITTED: handler, ExecutionPhase.SUBMITTING: handler},
                config=_config(),
                clock=clock,
                authority_verifier=_allow,
                notifier=delivered,
            )
        )
        clock.advance(5)

    # The delivered notice is still not resent: the suppression itself is intact.
    assert sends == 1
    row, actions = await _row_state(session_factory)
    notices = [action for action in actions if action.kind == "notify_execution_block"]
    assert [action.status for action in notices] == [ActionStatus.SUCCEEDED.value]

    # ...but the recurrence is durable, and carries the *current* instructions.
    assert reports[2].blocked == 1
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == BlockCode.HUMAN_REFUSED.value
    assert row.block_owner == "requester"
    assert row.block_required_input == "second occurrence"
    assert row.next_check_at is not None


async def test_a_stuck_block_keeps_its_wakeup_moving_and_cannot_starve_other_tenants(session_factory, execution):
    """A permanently blocked row must not hold the due queue against unrelated work.

    `next_check_at` only ever moves when a write happens, and `_load_due` orders by it
    ascending after resolving the oldest-due tenants first. A blocked execution whose
    notice needs no resend therefore kept its original wake-up and sorted ahead of
    everything on every later tick. At the action cap that is unbounded starvation, and
    it crosses the tenant boundary: one org's stuck human-gate block stopped another
    org's runnable execution from ever being examined again.
    """
    async with session_factory() as session:
        first = (await session.execute(select(OrchestrationExecution))).scalar_one()
        blocked_node_id = first.node_id
        other_flow = OrchestrationFlow(execution_paused=False, org_id="org-other", slug="other-flow", title="Other flow")
        session.add(other_flow)
        await session.flush()
        other_node = OrchestrationNode(
            org_id="org-other",
            flow_id=other_flow.id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="N9",
            kind="story",
            state="running",
            title="Other tenant node",
        )
        session.add(other_node)
        session.add(
            OrchestrationAcceptedPlan(
                org_id="org-other",
                flow_id=other_flow.id,
                version=1,
                plan_document={"execution_policy": _policy()},
                plan_hash="c" * 64,
            )
        )
        session.add(
            OrchestrationWorkClaim(
                id="claim-other",
                org_id="org-other",
                provider_repository_id=124,
                issue_number=5144,
                owner_kind=OwnerKind.ENGINE_FLOW.value,
                owner_ref=other_flow.id,
                state=ClaimState.HELD.value,
                generation=1,
                active_run_id="runner-other",
            )
        )
        await session.flush()
        # Due strictly after the blocked row, so only the stale wake-up can reorder them.
        await create_execution(
            session,
            identity=ExecutionIdentity("org-other", other_node.id, 1, 1, "claim-other", 1),
            flow_id=other_flow.id,
            next_check_at=NOW + timedelta(seconds=2),
        )
        await session.commit()

    class BlockOneTenant:
        """Blocks the first tenant's node forever; the other tenant wants to proceed."""

        def __init__(self) -> None:
            self.progressed: list[str] = []

        async def observe(self, context: RunnerContext) -> HandlerObservation:
            return HandlerObservation(ObservationKind.READY)

        def decide(self, context: RunnerContext, observation: HandlerObservation) -> HandlerDecision:
            if context.execution.node_id == blocked_node_id:
                return HandlerDecision(
                    DecisionKind.BLOCK,
                    block=BlockRecord(BlockCode.HUMAN_REFUSED, "requester", "awaiting a human decision"),
                )
            self.progressed.append(context.execution.node_id)
            return HandlerDecision(DecisionKind.WAIT)

        async def perform(self, context: RunnerContext, effect: EffectRequest) -> EffectResult:
            raise AssertionError("no provider effect is expected in this test")

    handler = BlockOneTenant()
    clock = FrozenClock()
    # One action per tick, so the ordering decides who runs at all.
    for _ in range(4):
        await run_execution_runner(
            session_factory,
            handlers={ExecutionPhase.ADMITTED: handler},
            config=_config(max_actions=1),
            clock=clock,
            authority_verifier=_allow,
            notifier=lambda _notification: "sns/stuck",
        )
        clock.advance(5)

    # The other tenant's execution was reached despite the permanent block.
    assert handler.progressed == [other_node.id]
    async with session_factory() as session:
        rows = {row.org_id: row for row in (await session.execute(select(OrchestrationExecution))).scalars()}
    # The block is still durable and still due — deferred, not abandoned.
    assert rows[ORG].status == ExecutionStatus.BLOCKED.value
    assert rows[ORG].block_code == BlockCode.HUMAN_REFUSED.value
    assert rows[ORG].next_check_at is not None
    # Its wake-up moved forward instead of pinning the head of the due queue.
    assert rows[ORG].next_check_at.replace(tzinfo=UTC) > NOW


async def _expire_policy_at(session_factory, expires_at: datetime) -> None:
    """Move the in-force policy's expiry, so authority can lapse mid-pass."""
    async with session_factory() as session:
        plan = (await session.execute(select(OrchestrationAcceptedPlan))).scalar_one()
        document = dict(plan.plan_document)
        policy = dict(document["execution_policy"])
        policy["expires_at"] = expires_at.isoformat()
        document["execution_policy"] = policy
        plan.plan_document = document
        await session.commit()


class ClockAdvancingHandler(SyntheticHandler):
    """Spends real time inside `observe`, as a provider round-trip does."""

    def __init__(self, *, clock: FrozenClock, observe_seconds: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self._clock = clock
        self._observe_seconds = observe_seconds

    async def observe(self, context: RunnerContext) -> HandlerObservation:
        observation = await super().observe(context)
        self._clock.advance(self._observe_seconds)
        return observation


async def test_authority_expiring_during_observation_stops_the_effect(session_factory, execution):
    """`observe` is bounded only by `io_timeout_seconds`, so authority can lapse inside it.

    The expiry must be judged at the authorization boundary, not against the
    timestamp captured before the runner went off to observe. Otherwise the check
    answers "was I authorized a while ago?" and the effect proceeds unauthorized.
    """
    clock = FrozenClock()
    await _expire_policy_at(session_factory, NOW + timedelta(seconds=2))
    handler = ClockAdvancingHandler(clock=clock, observe_seconds=3, decision=_effect_decision())

    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=clock,
        notifier=lambda _notification: "sns/authority-block",
    )

    assert handler.observe_count == 1, "observation itself must still be allowed"
    assert handler.perform_count == 0, "an effect must not be issued on lapsed authority"
    assert report.blocked == 1
    assert report.effects_attempted == 0

    row, actions = await _row_state(session_factory)
    # A durable typed block, not a silently dropped execution: `perform_count == 0`
    # alone would also pass against code that lost the work entirely.
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == BlockCode.AUTHORITY_UNVERIFIABLE.value
    assert row.block_owner == "plan-owner"
    assert row.block_required_input is not None
    assert row.next_check_at is not None, "a block must keep a due continuation"
    # Refused before the intent commit, so no provider intent was ever reserved.
    assert {action.kind for action in actions} == {"notify_execution_block"}


async def test_authority_expiring_between_intent_and_effect_stops_the_effect(session_factory, execution):
    """Authority is live at the intent commit and lapses in the gap before the call.

    This is the scenario the second, post-commit check exists for. The intent must
    survive for reconciliation while the effect is refused.
    """
    clock = FrozenClock()
    await _expire_policy_at(session_factory, NOW + timedelta(seconds=2))
    handler = SyntheticHandler(decision=_effect_decision())

    async def expire_after_intent(name, _context):
        if name == "after_intent":
            clock.advance(3)

    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=clock,
        notifier=lambda _notification: "sns/authority-block",
        checkpoint=expire_after_intent,
    )

    assert handler.perform_count == 0, "the provider must not be called on lapsed authority"
    assert report.blocked == 1
    assert report.effects_attempted == 0

    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == BlockCode.AUTHORITY_UNVERIFIABLE.value
    assert row.block_owner == "plan-owner"
    assert row.next_check_at is not None, "a block must keep a due continuation"
    # The reserved intent stays durable so reconciliation can still settle it.
    assert {action.kind for action in actions} == {"synthetic_provider_effect", "notify_execution_block"}
    effect = next(action for action in actions if action.kind == "synthetic_provider_effect")
    assert effect.status == ActionStatus.PREPARED.value


async def test_deadline_lapsing_during_observation_stops_the_effect(session_factory, execution):
    """The execution deadline is judged live, for the same reason authority is.

    A deadline that expires inside `observe` must be seen as lapsed. Against the
    loop-top capture it still reads as future, so the deadline fence is skipped and
    work proceeds past the limit it exists to enforce.
    """
    clock = FrozenClock()
    async with session_factory() as session:
        await session.execute(
            update(OrchestrationExecution).where(OrchestrationExecution.id == execution.id).values(deadline_at=NOW + timedelta(seconds=2))
        )
        await session.commit()

    handler = ClockAdvancingHandler(clock=clock, observe_seconds=3, decision=_effect_decision())
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=clock,
        authority_verifier=_allow,
        notifier=lambda _notification: "sns/deadline",
    )

    assert handler.observe_count == 1, "observation itself must still be allowed"
    assert handler.perform_count == 0, "no effect may be carried out past the deadline"
    assert report.blocked == 1
    row, actions = await _row_state(session_factory)
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == BlockCode.ATTEMPTS_EXHAUSTED.value
    assert row.next_check_at is not None, "a block must keep a due continuation"
    assert {action.kind for action in actions} == {"notify_execution_block"}


async def test_deadline_lapsing_between_intent_and_effect_stops_the_effect(session_factory, execution):
    """The deadline must also be judged in the gap between the intent commit and the call.

    The pre-observation deadline check cannot cover this window: it is asked before
    `observe`, which is bounded only by `io_timeout_seconds`, and more time passes
    while the intent is committed. A deadline that lapses after that single early
    reading is never noticed, so the runner carries out work past the limit the
    deadline exists to enforce.

    `verify_live_authority` does not close this gap either — it fences the accepted
    policy's `expires_at`, which is a different clock from the execution's own
    `deadline_at` and is never compared against it.
    """
    clock = FrozenClock()
    async with session_factory() as session:
        await session.execute(
            update(OrchestrationExecution).where(OrchestrationExecution.id == execution.id).values(deadline_at=NOW + timedelta(seconds=2))
        )
        await session.commit()

    handler = SyntheticHandler(decision=_effect_decision())

    async def lapse_after_intent(name, _context):
        if name == "after_intent":
            clock.advance(3)

    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=clock,
        authority_verifier=_allow,
        notifier=lambda _notification: "sns/deadline",
        checkpoint=lapse_after_intent,
    )

    assert handler.observe_count == 1, "observation itself must still be allowed"
    assert handler.perform_count == 0, "no effect may be carried out past the deadline"
    assert report.blocked == 1
    assert report.effects_attempted == 0

    row, actions = await _row_state(session_factory)
    # A durable typed block, not a silently dropped execution: `perform_count == 0`
    # alone would also pass against code that lost the work entirely.
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == BlockCode.ATTEMPTS_EXHAUSTED.value
    assert row.block_owner == "platform-operator"
    assert row.block_required_input is not None
    assert row.next_check_at is not None, "a block must keep a due continuation"
    # The intent committed before the refusal stays durable so reconciliation can
    # still settle whatever the provider may have been asked to do.
    assert {action.kind for action in actions} == {"synthetic_provider_effect", "notify_execution_block"}
    effect = next(action for action in actions if action.kind == "synthetic_provider_effect")
    assert effect.status == ActionStatus.PREPARED.value


@pytest.mark.parametrize("action", [Action.DEVELOP, Action.REVIEW, Action.REPAIR])
async def test_paused_flow_observes_but_never_reserves_or_performs(session_factory, execution, action):
    async with session_factory() as session:
        await session.execute(update(OrchestrationFlow).values(execution_paused=True))
        await session.commit()
    decision = _effect_decision()
    decision = replace(decision, effect=replace(decision.effect, action=action))
    handler = SyntheticHandler(decision=decision)
    clock = FrozenClock()
    for _ in range(3):
        report = await run_execution_runner(
            session_factory, handlers={ExecutionPhase.ADMITTED: handler}, config=_config(), clock=clock, authority_verifier=_allow
        )
        assert report.effects_attempted == 0 and report.reserved == 0 and report.errors == 0
        clock.advance(2)
    row, actions = await _row_state(session_factory)
    assert row.attempts == 0 and actions == []
    assert handler.observe_count == 3 and handler.perform_count == 0
    async with session_factory() as session:
        await session.execute(update(OrchestrationFlow).values(execution_paused=False))
        await session.commit()
    resumed = await run_execution_runner(
        session_factory, handlers={ExecutionPhase.ADMITTED: handler}, config=_config(), clock=clock, authority_verifier=_allow
    )
    assert resumed.effects_succeeded == 1
    row, actions = await _row_state(session_factory)
    assert row.attempts == 1 and len(actions) == 1


async def test_resume_does_not_reset_exhausted_attempts(session_factory, execution):
    async with session_factory() as session:
        await session.execute(update(OrchestrationFlow).values(execution_paused=True))
        await session.execute(update(OrchestrationExecution).values(attempts=3))
        await seed_stage_attempts(session, execution, Action.DEVELOP, 3)
        await session.commit()
    handler = SyntheticHandler(decision=_effect_decision())
    clock = FrozenClock()
    await run_execution_runner(session_factory, handlers={ExecutionPhase.ADMITTED: handler}, config=_config(), clock=clock, authority_verifier=_allow)
    async with session_factory() as session:
        await session.execute(update(OrchestrationFlow).values(execution_paused=False))
        await session.commit()
    clock.advance(2)
    report = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(),
        clock=clock,
        authority_verifier=_allow,
        notifier=lambda _: "test-notice",
    )
    row, actions = await _row_state(session_factory)
    assert report.blocked == 1 and handler.perform_count == 0
    assert row.attempts == 3 and row.block_code == BlockCode.ATTEMPTS_EXHAUSTED.value
    assert all(action.kind != "synthetic_provider_effect" for action in actions)


async def test_completed_work_reconciles_while_flow_is_paused(session_factory, execution):
    async with session_factory() as session:
        await session.execute(update(OrchestrationFlow).values(execution_paused=True))
        await session.commit()
    handler = SyntheticHandler(decision=HandlerDecision(DecisionKind.CONCLUDE, phase=ExecutionPhase.CONCLUDED))
    report = await run_execution_runner(
        session_factory, handlers={ExecutionPhase.ADMITTED: handler}, config=_config(), clock=FrozenClock(), authority_verifier=_allow
    )
    row, actions = await _row_state(session_factory)
    assert report.errors == 0 and row.status == "concluded"
    assert row.attempts == 0 and actions == [] and handler.perform_count == 0


async def seed_stage_attempts(session, execution, action, count):
    for index in range(count):
        session.add(
            OrchestrationAction(
                org_id=execution.org_id,
                execution_id=execution.id,
                operation_key=f"history:{action.value}:{index}",
                kind="historical_effect",
                status="failed",
                attempt=index + 1,
                detail={"attempt_stage": action.value},
                created_at=NOW,
            )
        )
    await session.flush()


async def test_exhausted_stage_does_not_consume_another_stage(session_factory, execution):
    from src.orchestration.stage_attempts import stage_attempts

    async with session_factory() as session:
        await seed_stage_attempts(session, execution, Action.DEVELOP, 3)
        await seed_stage_attempts(session, execution, Action.REVIEW, 3)
        await session.commit()
        assert await stage_attempts(session, org_id=ORG, node_id=execution.node_id, action=Action.REVIEW) == 3
        assert await stage_attempts(session, org_id=ORG, node_id=execution.node_id, action=Action.MERGE) == 0
    # The default plan permits repair; development exhaustion cannot block it.
    repair = EffectRequest(ActionIntent("repair:first", "repair"), Action.REPAIR)
    assert await verify_live_authority(session_factory, execution, repair, NOW) is None
    handler = SyntheticHandler(decision=HandlerDecision(DecisionKind.EFFECT, effect=repair), effect_result=EffectResult(EffectOutcome.SUCCEEDED))
    result = await run_execution_runner(
        session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=_config(max_attempts=1),
        clock=FrozenClock(),
        authority_verifier=verify_live_authority,
    )
    assert result.effects_succeeded == 1
