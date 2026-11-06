"""Real SQL/Moto dispatch of an operator-bound PR after three failed developers."""

# ruff: noqa: F811

import asyncio
import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration.dispatch_pass import PendingPublish, _build_envelope, attempt_run_id
from src.orchestration.execution_state import BlockCode, ExecutionPhase, ExecutionStatus, PhaseAdvance
from src.orchestration.execution_store import advance_execution
from src.orchestration.genesis import resolve_engine_genesis
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
)
from src.orchestration.protected_review_recovery import request_recovery
from src.orchestration.retained_work_recovery import require_budget
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.review_recovery import ReviewRecoveryRequest
from src.orchestration.work_claims import ReleaseReason, release_work
from tests.orchestration.test_review_cycle import ORG, REPO, cycle, pg_server, pg_url, state, store, tick  # noqa: F401

pytestmark = pytest.mark.parametrize("cycle", [{"missing_handoff": True, "node_attempt": 3, "max_attempts": 3}], indirect=True)


def freeze_recovery_clock(monkeypatch, *, at=None):
    from src.orchestration import released_reviewer_recovery, retained_work_recovery

    # Routine recovery scenarios must not cross an absolute preview window merely
    # because CI reaches a quarter hour. Keep the clock inside the current window;
    # explicit expiry/renewal tests still advance the patched clock themselves.
    window = retained_work_recovery.WINDOW_SECONDS
    if at is None:
        at = datetime.fromtimestamp((int(datetime.now(UTC).timestamp()) // window) * window + window // 2, UTC)
    moment = [at]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment[0].astimezone(tz) if tz is not None else moment[0].replace(tzinfo=None)

    monkeypatch.setattr(retained_work_recovery, "datetime", Clock)
    monkeypatch.setattr(released_reviewer_recovery, "datetime", Clock)
    return moment


async def failed_work(ctx, monkeypatch, *, source_dispatch_actor="system:orchestration-dispatch"):
    freeze_recovery_clock(monkeypatch)
    # Existing financial meters are read through a narrow offline fixture. The
    # production budget guard itself is exercised separately below.
    monkeypatch.setattr(
        "src.orchestration.retained_work_recovery.require_budget",
        AsyncMock(
            return_value={
                "chain_cap_usd": "150",
                "run_cap_usd": "100",
                "flow_cap_usd": "500",
            }
        ),
    )
    async with ctx.factory() as db:
        genesis = await resolve_engine_genesis(db, org_id=ORG, decision_id=ctx.approval.id)
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        plan.accepted_by_decision_id = ctx.approval.id
        binding = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
        binding.registered_by_kind, binding.registered_by = "human", "human"
        binding.recovery_reason = "Owner binds exact provider PR and head to retained original issue work."
        node = await db.get(OrchestrationNode, ctx.node.id)
        await advance_execution(
            db,
            identity=ctx.identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.CONCLUDED,
                status=ExecutionStatus.CONCLUDED,
                expected_revision=ctx.execution.revision,
            ),
        )
        node.state = "failed"
        await release_work(
            db, org_id=ORG, claim_id=ctx.claim.id, generation=5, reason=ReleaseReason.FAILED, terminal_evidence="protected failed:" + ctx.root
        )
        for attempt in (1, 2, 3):
            run_id = attempt_run_id(node.id, attempt)
            db.add(
                OrchestrationDecision(
                    org_id=ORG,
                    flow_id=ctx.flow.id,
                    node_id=node.id,
                    kind="node_dispatched",
                    actor_kind="service",
                    actor_id=source_dispatch_actor if attempt == 1 else "system:orchestration-dispatch",
                    actor_role="engine",
                    reason=json.dumps(dict(run_id=run_id, attempt=attempt, repo=REPO, issue=43)),
                )
            )
        await db.commit()
    current = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{ctx.root}")
    chain = current["flow_id"]["S"]
    before = {}
    for attempt in (1, 2, 3):
        run_id = attempt_run_id(ctx.node.id, attempt)
        if attempt != 3:
            original = copy.copy(ctx.node)
            original.attempts = attempt
            envelope = _build_envelope(
                node=original,
                genesis=genesis,
                graph_address="cycle/E1/W1/N1",
                installation_id=42,
                issue=43,
                config=ctx.service.config,
                user_id="human",
                cognito_sub="sub",
            )
            envelope["source_ref"]["provider_repository_id"] = 123
            envelope["correlation"]["correlation_id"] = chain
            ctx.service.writer.provision(
                PendingPublish(
                    node_id=ctx.node.id,
                    org_id=ORG,
                    envelope=envelope,
                    group_id="cycle",
                    deduplication_id=run_id,
                    genesis=genesis,
                    node_attempt=attempt,
                )
            )
        raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{run_id}")
        raw.update(status={"S": "completed"}, terminal_outcome={"S": "failed"}, workload_binding={"S": f"exited-{attempt}"})
        ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
        before[run_id] = raw
    ctx.source = attempt_run_id(ctx.node.id, 1)
    request = ReviewRecoveryRequest(
        mode="review_existing_work",
        source_run_id=ctx.source,
        expected_attempt=3,
        expected_plan_version=ctx.plan.version,
        expected_run_id=ctx.root,
        expected_head_sha=ctx.head,
        reason="Owner authorizes real review of this exact retained original PR after budget failures.",
    )
    return request, before


async def call(ctx, request, *, accept=False):
    async with ctx.factory() as db:
        result = await request_recovery(
            db, org_id=ORG, node_id=ctx.node.id, actor_id="human", actor_role="owner", request=request, accept=accept, services=ctx.service
        )
        if accept:
            await db.commit()
        return result


async def test_one_real_review_preserves_three_failures_attempts_claim_and_original_chain(cycle, monkeypatch):
    request, protected = await failed_work(cycle, monkeypatch)
    before = await state(cycle)
    preview = await call(cycle, request)
    assert preview["source_run_id"] == cycle.source and preview["prior_run_id"] == cycle.root
    assert (await state(cycle))[1].state == "released" and not cycle.calls
    request.expected_snapshot = preview["snapshot"]
    accepted = await asyncio.gather(call(cycle, request, accept=True), call(cycle, request, accept=True))
    assert sorted(result["created"] for result in accepted) == [False, True]
    execution, claim, node, actions = await state(cycle)
    assert node.attempts == 3 and node.state == "running" and not actions
    assert execution.id == before[0].id and execution.attempts == before[0].attempts and execution.handoff_receipt_ref is None
    assert claim.id == before[1].id and claim.generation == before[1].generation + 1
    cycle.identity = replace(cycle.identity, claim_generation=claim.generation)
    result = await tick(cycle)
    assert result.effects_succeeded == 1, (result, (await state(cycle))[0].block_detail)
    execution, claim, node, actions = await state(cycle)
    assert len(cycle.calls) == 1 and cycle.calls[0]["persona"] == "agent-codex-reviewer"
    assert cycle.calls[0]["correlation"]["correlation_id"] == preview["correlation_id"]
    assert actions[0].detail["source_run_id"] == cycle.source
    assert node.attempts == 3 and execution.handoff_receipt_ref is None
    assert not cycle.calls[0]["review_cycle_input"]["allow_story_repairs"]
    assert {run: cycle.store._read(f"TENANT#{ORG}", f"EXEC#{run}") for run in protected} == protected
    assert (await call(cycle, request, accept=True))["created"] is False


@pytest.mark.parametrize("foreign_handoff", [False, True])
async def test_failed_developer_handoff_is_preserved_during_retained_review(cycle, monkeypatch, foreign_handoff):
    from src.orchestration.handoff import handoff_receipt_ref
    from src.orchestration.models import OrchestrationExecution

    request, protected = await failed_work(cycle, monkeypatch)
    receipt = handoff_receipt_ref(cycle.identity, cycle.execution.id)
    async with cycle.factory() as db:
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        execution.handoff_receipt_ref = "handoff/foreign" if foreign_handoff else receipt
        await db.commit()
    if foreign_handoff:
        with pytest.raises(CycleBlockedError, match="retained_execution_not_settled"):
            await call(cycle, request)
        return
    preview = await call(cycle, request)
    assert preview["handoff_receipt_ref"] == receipt
    request.expected_snapshot = preview["snapshot"]
    await call(cycle, request, accept=True)
    execution, claim, node, _ = await state(cycle)
    assert execution.handoff_receipt_ref == receipt and node.attempts == 3
    cycle.identity = replace(cycle.identity, claim_generation=claim.generation)
    result = await tick(cycle)
    assert result.effects_succeeded == 1, (result, (await state(cycle))[0].block_detail)
    assert len(cycle.calls) == 1 and cycle.calls[0]["persona"] == "agent-codex-reviewer"
    assert (await state(cycle))[0].handoff_receipt_ref == receipt
    assert {run: cycle.store._read(f"TENANT#{ORG}", f"EXEC#{run}") for run in protected} == protected


@pytest.mark.parametrize("change", ["head", "binding", "source", "effect", "budget", "actor", "expiry"])
async def test_changed_preview_never_reacquires_or_dispatches(cycle, monkeypatch, change):
    request, _ = await failed_work(cycle, monkeypatch)
    request.expected_snapshot = (await call(cycle, request))["snapshot"]
    if change == "head":
        cycle.head = "d" * 40
    elif change == "source":
        raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.source}")
        raw["status"] = {"S": "active"}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    elif change == "budget":
        monkeypatch.setattr(
            "src.orchestration.retained_work_recovery.require_budget", AsyncMock(side_effect=CycleBlockedError("retained_budget_exhausted"))
        )
    elif change == "actor":
        monkeypatch.setattr("src.orchestration.protected_developer_recovery.policy_owner_matches", AsyncMock(return_value=False))
    elif change == "expiry":
        monkeypatch.setattr("src.orchestration.retained_work_recovery.WINDOW_SECONDS", 1)
    else:
        async with cycle.factory() as db:
            if change == "binding":
                binding = await db.get(OrchestrationPullRequestBinding, cycle.binding.id)
                binding.revision += 1
            else:
                db.add(
                    OrchestrationAction(
                        org_id=ORG, execution_id=cycle.execution.id, operation_key="unknown-effect", kind="provider-write", status="unknown"
                    )
                )
            await db.commit()
    with pytest.raises(CycleBlockedError):
        await call(cycle, request, accept=True)
    execution, claim, node, _ = await state(cycle)
    assert claim.state == "released" and claim.generation == 5 and node.state == "failed" and node.attempts == 3
    assert execution.status == "concluded" and not cycle.calls


@pytest.mark.parametrize("failure", ["provision", "queue"])
async def test_partial_dispatch_replays_one_reserved_review(cycle, monkeypatch, failure):
    request, _ = await failed_work(cycle, monkeypatch)
    request.expected_snapshot = (await call(cycle, request))["snapshot"]
    await call(cycle, request, accept=True)
    cycle.identity = replace(cycle.identity, claim_generation=6)
    target, name = (cycle.service, "provision") if failure == "provision" else (cycle.service.queue, "send_message")
    original = getattr(target, name)
    attempted = []

    def once(*args, **kwargs):
        attempted.append(True)
        if len(attempted) == 1:
            raise RuntimeError("offline uncertain transport")
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, once)
    await tick(cycle)
    first = await state(cycle)
    await tick(cycle)
    second = await state(cycle)
    assert len(second[3]) == 1 and second[3][0].operation_key == first[3][0].operation_key
    assert second[2].attempts == 3 and len(cycle.calls) == 1


@pytest.mark.parametrize("total,available", [(Decimal(49), True), (Decimal(51), False), (None, False)])
async def test_chain_guard_uses_existing_meter_and_unchanged_caps(cycle, monkeypatch, total, available):
    from src.budget.enforcement_service import BudgetEnforcementService

    caps = AsyncMock(side_effect=[Decimal(100), Decimal(150)])
    monkeypatch.setattr(BudgetEnforcementService, "_resolve_scope_cap", caps)
    meter = SimpleNamespace(snapshot=AsyncMock(return_value=None if total is None else SimpleNamespace(total_usd=total)))
    monkeypatch.setattr("src.orchestration.flow_budget.get_flow_reservations", lambda: meter)
    policy = SimpleNamespace(_shared_run_spend_usd=None, _shared_chain_spend_usd=None, limits=SimpleNamespace(max_spend_usd=Decimal(500)))
    args = dict(
        node=SimpleNamespace(org_id="org"),
        raw={"flow_id": {"S": "original-chain"}},
        policy=policy,
        meter=SimpleNamespace(total_usd=Decimal(100)),
    )
    if available:
        limits = await require_budget(None, **args)
        assert limits == {"chain_cap_usd": "150", "run_cap_usd": "100", "flow_cap_usd": "500"}
    else:
        with pytest.raises(CycleBlockedError) as error:
            await require_budget(None, **args)
        if total is None:
            assert error.value.reason == "retained_budget_unavailable" and error.value.code is not BlockCode.BUDGET_EXHAUSTED
        else:
            assert error.value.reason == "retained_budget_exhausted" and error.value.code is BlockCode.BUDGET_EXHAUSTED
    target = meter.snapshot.call_args.args[0]
    assert target.entity_id == "original-chain" and target.headroom_usd == Decimal(150)


@pytest.mark.parametrize(
    "entries,expected",
    [
        ({"request": "49:2000"}, Decimal(49)),
        ({}, None),
        ({"request": "bad:2000"}, None),
        ({"request": "-1:2000"}, None),
        ({"request": "1:nan"}, None),
        ({"request": "1:Infinity"}, None),
        ({"request": "1:bad"}, None),
        ({"__initialized__": "0:2000"}, None),
        ({"__initialized__": "0:bad", "request": "1:2000"}, None),
        ({"pending:request": "0:2000"}, None),
        ({"bounded:request": "0:2000"}, None),
        ({"pending:missing": "0:2000", "request": "1:2000"}, None),
        ({"pending:request": "1:2000", "request": "1:2000"}, None),
        ({"pending:request": "0:999", "request": "20:2000"}, None),
        ({"unbounded:request": "0:999"}, None),
        ({"unbounded:request": "0:2000", "request": "1:2000"}, None),
    ],
)
async def test_existing_chain_snapshot_never_initializes_or_zeros_unknown_usage(cycle, monkeypatch, entries, expected):
    from src.budget.reservations import ReservationStore, ReservationTarget

    client = SimpleNamespace(hgetall=AsyncMock(return_value=entries))
    store = ReservationStore(None, 3600)
    monkeypatch.setattr(store, "_get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(store, "_clock", lambda: 1000)
    target = ReservationTarget(ORG, "chain", "original-chain", "run", "lifetime", Decimal(150))
    strict = await store.snapshot(target)
    if "__initialized__" not in entries:
        assert strict is None  # Existing strict readers stay strict.
    observed = await store.snapshot(target, existing_scope=True)
    assert (observed.total_usd if observed is not None else None) == expected
    assert await store.snapshot(replace(target, entity_type="flow"), existing_scope=True) is None
    assert client.hgetall.call_count == 2


@pytest.mark.parametrize("change", ["head", "expiry", "budget"])
async def test_acceptance_is_not_a_permanent_dispatch_bypass(cycle, monkeypatch, change):
    from src.orchestration import retained_work_recovery

    request, _ = await failed_work(cycle, monkeypatch)
    request.expected_snapshot = (await call(cycle, request))["snapshot"]
    await call(cycle, request, accept=True)
    cycle.identity = replace(cycle.identity, claim_generation=6)
    if change == "head":
        cycle.head = "d" * 40
    elif change == "expiry":

        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz or UTC) + timedelta(seconds=901)

        monkeypatch.setattr(retained_work_recovery, "datetime", Later)
    else:
        monkeypatch.setattr(retained_work_recovery, "require_budget", AsyncMock(side_effect=CycleBlockedError("retained_budget_exhausted")))
    assert (await tick(cycle)).effects_attempted == 0
    assert not cycle.calls and (await state(cycle))[2].attempts == 3


async def test_preview_window_boundary_requires_a_fresh_snapshot(cycle, monkeypatch):
    from src.orchestration import retained_work_recovery

    request, _ = await failed_work(cycle, monkeypatch)
    window = retained_work_recovery.WINDOW_SECONDS
    boundary = datetime.fromtimestamp((int(datetime.now(UTC).timestamp()) // window + 1) * window, UTC)
    moment = freeze_recovery_clock(monkeypatch, at=boundary - timedelta(milliseconds=250))
    before = await call(cycle, request)
    request.expected_snapshot = before["snapshot"]

    moment[0] = boundary + timedelta(milliseconds=250)
    with pytest.raises(CycleBlockedError) as error:
        await call(cycle, request, accept=True)
    assert error.value.reason == "recovery_preview_changed"
    after = await call(cycle, request)
    assert {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)} == {"expires_at", "snapshot"}
    _, claim, node, _ = await state(cycle)
    assert claim.state == "released" and claim.generation == 5 and node.attempts == 3
    assert not cycle.calls


async def test_expiry_after_provision_never_publishes_the_accepted_recovery(cycle, monkeypatch):
    from src.orchestration import retained_work_recovery

    request, _ = await failed_work(cycle, monkeypatch)
    window = retained_work_recovery.WINDOW_SECONDS
    boundary = datetime.fromtimestamp((int(datetime.now(UTC).timestamp()) // window + 1) * window, UTC)
    moment = freeze_recovery_clock(monkeypatch, at=boundary - timedelta(milliseconds=250))
    request.expected_snapshot = (await call(cycle, request))["snapshot"]
    await call(cycle, request, accept=True)
    cycle.identity = replace(cycle.identity, claim_generation=6)
    provision = cycle.service.provision

    def provision_then_expire(*args, **kwargs):
        result = provision(*args, **kwargs)
        moment[0] = boundary + timedelta(milliseconds=250)
        return result

    monkeypatch.setattr(cycle.service, "provision", provision_then_expire)
    result = await tick(cycle)
    assert result.effects_attempted == 1 and result.effects_succeeded == 0
    _, claim, node, actions = await state(cycle)
    assert len(actions) == 1 and actions[0].status == "unknown"
    assert claim.generation == 6 and node.attempts == 3 and not cycle.calls


async def test_provider_head_change_after_provision_never_publishes(cycle, monkeypatch):
    request, _ = await failed_work(cycle, monkeypatch)
    request.expected_snapshot = (await call(cycle, request))["snapshot"]
    await call(cycle, request, accept=True)
    cycle.identity = replace(cycle.identity, claim_generation=6)
    original = cycle.service.provision

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        cycle.head = "d" * 40
        return result

    monkeypatch.setattr(cycle.service, "provision", race)
    result = await tick(cycle)
    assert result.effects_succeeded == 0 and not cycle.calls
    assert (await state(cycle))[2].attempts == 3


def move_clock(monkeypatch, *, seconds=901):
    from src.orchestration import retained_work_recovery

    current = retained_work_recovery.datetime

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.now(tz or UTC) + timedelta(seconds=seconds)

    monkeypatch.setattr(retained_work_recovery, "datetime", Later)


async def accepted_work(cycle, monkeypatch, *, retained_handoff=False):
    request, protected = await failed_work(cycle, monkeypatch)
    if retained_handoff:
        from src.orchestration.handoff import handoff_receipt_ref
        from src.orchestration.models import OrchestrationExecution

        async with cycle.factory() as db:
            execution = await db.get(OrchestrationExecution, cycle.execution.id)
            execution.handoff_receipt_ref = handoff_receipt_ref(cycle.identity, cycle.execution.id)
            await db.commit()
    request.expected_snapshot = (await call(cycle, request))["snapshot"]
    accepted = await call(cycle, request, accept=True)
    cycle.identity = replace(cycle.identity, claim_generation=6)
    request.renew_decision_id = accepted["decision_id"]
    request.expected_snapshot = None
    return request, protected


async def leave_unstarted(cycle, monkeypatch):
    original = cycle.service.prepare
    monkeypatch.setattr(cycle.service, "prepare", AsyncMock(side_effect=RuntimeError("offline failure before committed dispatch")))
    await tick(cycle)
    monkeypatch.setattr(cycle.service, "prepare", original)


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("retained_handoff", [False, True])
async def test_expired_unstarted_review_renews_once_without_resetting_or_replacing_intent(cycle, monkeypatch, pending, retained_handoff):
    request, protected = await accepted_work(cycle, monkeypatch, retained_handoff=retained_handoff)
    async with cycle.factory() as db:
        original = (await db.get(OrchestrationDecision, request.renew_decision_id)).reason
    if pending:
        await leave_unstarted(cycle, monkeypatch)
    move_clock(monkeypatch)
    await tick(cycle)  # A normal observer sees the expired receipt first.
    before = await state(cycle)
    preview = await call(cycle, request)
    request.expected_snapshot = preview["snapshot"]
    results = await asyncio.gather(call(cycle, request, accept=True), call(cycle, request, accept=True))
    assert sorted(result["created"] for result in results) == [False, True]
    renewed = await state(cycle)
    assert renewed[0].handoff_receipt_ref == before[0].handoff_receipt_ref
    assert renewed[0].attempts == before[0].attempts and renewed[2].attempts == 3
    assert renewed[1].id == before[1].id and renewed[1].generation == before[1].generation == 6
    assert [a.operation_key for a in renewed[3]] == [a.operation_key for a in before[3]]
    assert (await tick(cycle)).effects_succeeded == 1
    assert len(cycle.calls) == 1
    assert (await state(cycle))[0].handoff_receipt_ref == before[0].handoff_receipt_ref
    if pending:
        assert (await state(cycle))[3][0].operation_key == before[3][0].operation_key
    async with cycle.factory() as db:
        assert (await db.get(OrchestrationDecision, request.renew_decision_id)).reason == original
    assert {run: cycle.store._read(f"TENANT#{ORG}", f"EXEC#{run}") for run in protected} == protected


@pytest.mark.parametrize("retained_handoff,replacement", [(False, "handoff/foreign"), (True, "handoff/foreign"), (True, None)])
async def test_renewal_rejects_changed_handoff_history(cycle, monkeypatch, retained_handoff, replacement):
    from src.orchestration.models import OrchestrationExecution

    request, _ = await accepted_work(cycle, monkeypatch, retained_handoff=retained_handoff)
    move_clock(monkeypatch)
    request.expected_snapshot = (await call(cycle, request))["snapshot"]
    async with cycle.factory() as db:
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        execution.handoff_receipt_ref = replacement
        await db.commit()
    for accept in (False, True):
        with pytest.raises(CycleBlockedError, match="retained_renewal_requires_reconciliation"):
            await call(cycle, request, accept=accept)
    execution, claim, node, _ = await state(cycle)
    assert execution.handoff_receipt_ref == replacement and claim.generation == 6 and node.attempts == 3
    assert not cycle.calls


@pytest.mark.parametrize("boundary", ["receipt", "protected", "queue", "claim"])
async def test_renewal_refuses_when_dispatch_requires_reconciliation(cycle, monkeypatch, boundary):
    from src.orchestration.models import OrchestrationWorkClaim
    from src.orchestration.review_cycle_dispatch import continuation_run_id

    request, _ = await accepted_work(cycle, monkeypatch)
    if boundary in {"receipt", "queue"}:
        target, name = (cycle.service, "provision") if boundary == "receipt" else (cycle.service.queue, "send_message")
        monkeypatch.setattr(target, name, lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("uncertain offline transport")))
        await tick(cycle)
    elif boundary == "protected":
        await leave_unstarted(cycle, monkeypatch)
        action = (await state(cycle))[3][0]
        run_id = continuation_run_id(action.operation_key)
        raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}")
        raw.update(sk={"S": f"EXEC#{run_id}"}, invocation_id={"S": run_id}, status={"S": "pending"})
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    else:
        async with cycle.factory() as db:
            claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
            claim.active_run_id = claim.claim_event_id = "different-owner"
            await db.commit()
    move_clock(monkeypatch)
    with pytest.raises(CycleBlockedError, match="retained_renewal_requires_reconciliation"):
        await call(cycle, request)
    assert not cycle.calls and (await state(cycle))[2].attempts == 3


@pytest.mark.parametrize("change", ["head", "source", "budget", "expired_snapshot"])
async def test_renewal_rechecks_current_snapshot_before_acceptance(cycle, monkeypatch, change):
    request, _ = await accepted_work(cycle, monkeypatch)
    move_clock(monkeypatch)
    request.expected_snapshot = (await call(cycle, request))["snapshot"]
    if change == "head":
        cycle.head = "d" * 40
    elif change == "source":
        raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.source}")
        raw["terminal_outcome"] = {"S": "complete"}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    elif change == "budget":
        monkeypatch.setattr(
            "src.orchestration.retained_work_recovery.require_budget", AsyncMock(side_effect=CycleBlockedError("retained_budget_exhausted"))
        )
    else:
        move_clock(monkeypatch)
    with pytest.raises(CycleBlockedError):
        await call(cycle, request, accept=True)
    assert not cycle.calls and (await state(cycle))[1].generation == 6


@pytest.mark.parametrize("change", ["head", "source", "budget"])
async def test_claim_transfer_repeats_current_head_source_and_budget_guards(cycle, monkeypatch, change):
    from src.orchestration import work_claims

    await accepted_work(cycle, monkeypatch)
    original = work_claims.continue_run

    async def race(*args, **kwargs):
        if change == "head":
            cycle.head = "d" * 40
        elif change == "source":
            raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.source}")
            raw["status"] = {"S": "active"}
            cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
        else:
            monkeypatch.setattr(
                "src.orchestration.retained_work_recovery.require_budget", AsyncMock(side_effect=CycleBlockedError("retained_budget_exhausted"))
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(work_claims, "continue_run", race)
    assert (await tick(cycle)).effects_succeeded == 0
    assert not cycle.calls and (await state(cycle))[1].active_run_id.startswith("recovery:")


async def test_expiry_during_last_await_never_publishes(cycle, monkeypatch):
    from src.agentauth import model_policy

    await accepted_work(cycle, monkeypatch)
    original = model_policy.ensure_snapshot_report_only

    async def race(*args, **kwargs):
        result = await original(*args, **kwargs)
        move_clock(monkeypatch)
        return result

    monkeypatch.setattr(model_policy, "ensure_snapshot_report_only", race)
    assert (await tick(cycle)).effects_succeeded == 0
    assert not cycle.calls


@pytest.mark.parametrize("boundary", ["preparation", "publication"])
async def test_budget_change_during_dispatch_persists_actionable_budget_progress(cycle, monkeypatch, boundary):
    from src.budget.enforcement_service import BudgetEnforcementService
    from src.orchestration.delivery_progress import node_progress
    from src.shared.schemas.budget import EntityType

    await accepted_work(cycle, monkeypatch)

    async def cap(_self, _session, _org_id, entity_type, **kwargs):
        return Decimal(100) if entity_type is EntityType.RUN else Decimal(150)

    def exhaust():
        # Switch the accepted fixture's budget observation to the production
        # guard reading exhausted original-chain accounting at this boundary.
        ledger = SimpleNamespace(snapshot=AsyncMock(return_value=SimpleNamespace(total_usd=Decimal(151))))
        monkeypatch.setattr(BudgetEnforcementService, "_resolve_scope_cap", cap)
        monkeypatch.setattr("src.orchestration.flow_budget.get_flow_reservations", lambda: ledger)
        monkeypatch.setattr("src.orchestration.retained_work_recovery.require_budget", require_budget)

    if boundary == "preparation":
        original = cycle.service.prepare

        async def before_prepare(*args, **kwargs):
            exhaust()
            return await original(*args, **kwargs)

        monkeypatch.setattr(cycle.service, "prepare", before_prepare)
    else:
        original = cycle.service.provision

        def after_provision(*args, **kwargs):
            result = original(*args, **kwargs)
            exhaust()
            return result

        monkeypatch.setattr(cycle.service, "provision", after_provision)

    result = await tick(cycle)
    row, claim, node, actions = await state(cycle)
    assert result.blocked == 1 and result.effects_uncertain == 1
    assert row.status == "blocked" and row.block_code == "budget_exhausted" and row.block_owner == "budget-admin"
    assert "chain or flow" in row.block_detail and "run cap" in row.block_detail
    assert "financial approval" in row.block_required_input and "separately audited recovery" in row.block_required_input
    assert row.pending_action_key == actions[0].operation_key and actions[0].status == "unknown"
    assert row.next_check_at is not None and not cycle.calls and node.attempts == 3 and claim.generation == 6
    progress = node_progress(node=node, binding=cycle.binding, dispatch={}, result={}, execution=row, policy_enabled=True, plan_version=1)
    assert progress.blocker == "budget_exhausted" and progress.automation == "blocked" and progress.next_action == row.block_required_input
    assert "Reviewing the current PR head" not in progress.detail
    await tick(cycle)
    again = await state(cycle)
    assert len(again[3]) == 1 and again[0].pending_action_key == row.pending_action_key
    assert again[0].block_code == "budget_exhausted" and not cycle.calls


@pytest.mark.parametrize("change", ["tenant", "provider", "issue", "dispatch_actor", "claim_generation", "revoked"])
async def test_original_assignment_requires_exact_historical_lineage(cycle, monkeypatch, change):
    from src.orchestration.models import OrchestrationWorkClaim

    actor = "different-service" if change == "dispatch_actor" else "system:orchestration-dispatch"
    request, _ = await failed_work(cycle, monkeypatch, source_dispatch_actor=actor)
    if change in {"tenant", "provider", "issue"}:
        raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.source}")
        key, value = {
            "tenant": ("tenant_id", {"S": "other-tenant"}),
            "provider": ("provider_repository_id", {"N": "999"}),
            "issue": ("issue_number", {"N": "999"}),
        }[change]
        raw[key] = value
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    elif change == "revoked":
        raw = cycle.store._read(f"TENANT#{ORG}", f"GRANT#{cycle.source}#1")
        raw["revoked"] = {"BOOL": True}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    elif change == "claim_generation":
        async with cycle.factory() as db:
            (await db.get(OrchestrationWorkClaim, cycle.claim.id)).generation += 1
            await db.commit()
    with pytest.raises(CycleBlockedError):
        await call(cycle, request)
    assert not cycle.calls and (await state(cycle))[2].attempts == 3
