"""Real PostgreSQL recovery of a released developer, without fabricated handoff."""

# ruff: noqa: F811

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.execution_state import OutcomeKind
from src.orchestration.execution_store import load_execution
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.protected_developer_recovery import KIND, reservation_id
from src.orchestration.protected_review_recovery import request_recovery
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.review_cycle_dispatch import receipt_id
from src.orchestration.review_recovery import ReviewRecoveryRequest
from src.orchestration.work_claims import ClaimBinding, ClaimOwner, Disposition, OwnerKind, ReleaseReason, claim_work, release_work
from tests.orchestration.test_review_cycle import ORG, cycle, pg_server, pg_url, state, store, tick  # noqa: F401

pytestmark = pytest.mark.parametrize("cycle", [{"missing_handoff": True, "plan_version": 2}], indirect=True)


async def stranded(ctx, *, held=False):
    raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{ctx.root}")
    raw["workload_binding"] = {"S": "pod:completed-developer"}
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    async with ctx.factory() as db:
        if held:
            req = ReviewRecoveryRequest(
                expected_attempt=1,
                expected_plan_version=ctx.plan.version,
                expected_run_id=ctx.root,
                expected_head_sha=ctx.head,
                reason="Owner authorizes first review after verified developer exit without handoff.",
            )
            return req, raw
        released = await release_work(
            db,
            org_id=ORG,
            claim_id=ctx.claim.id,
            generation=5,
            reason=ReleaseReason.COMPLETED,
            terminal_evidence="protected terminal report:" + ctx.root,
        )
        assert released.disposition is Disposition.ADMITTED
        await db.commit()
    req = ReviewRecoveryRequest(
        expected_attempt=1,
        expected_plan_version=ctx.plan.version,
        expected_run_id=ctx.root,
        expected_head_sha=ctx.head,
        reason="Owner authorizes first review of delivered work after premature release.",
    )
    return req, raw


async def call(ctx, req, *, accept=False, actor_id="human"):
    async with ctx.factory() as db:
        value = await request_recovery(
            db, org_id=ORG, node_id=ctx.node.id, actor_id=actor_id, actor_role="owner", request=req, accept=accept, services=ctx.service
        )
        if accept:
            await db.commit()
        return value


async def accept(ctx):
    req, raw = await stranded(ctx)
    preview = await call(ctx, req)
    req.expected_snapshot = preview["snapshot"]
    accepted = await call(ctx, req, accept=True)
    ctx.identity = replace(ctx.identity, claim_generation=6)
    return req, raw, accepted


async def test_reacquires_once_and_dispatches_real_reviewer_preserving_delivery(cycle):
    req, raw = await stranded(cycle)
    before = await state(cycle)
    # This is the production failure: the runner never selects the released lane.
    assert (await tick(cycle, force=False)).effects_attempted == 0
    preview = await call(cycle, req)
    assert preview["claim_generation"] == 6 and preview["prior_claim_generation"] == 5
    assert (await state(cycle))[1].state == "released" and not cycle.calls
    req.expected_snapshot = preview["snapshot"]
    results = await asyncio.gather(call(cycle, req, accept=True), call(cycle, req, accept=True))
    assert sorted(r["created"] for r in results) == [False, True]
    accepted = next(r for r in results if r["created"])
    execution, claim, node, actions = await state(cycle)
    assert claim.generation == 6 and claim.active_run_id == reservation_id(accepted["decision_id"])
    assert execution.id == before[0].id and execution.handoff_receipt_ref is None and execution.attempts == before[0].attempts
    assert node.attempts == 1 and not actions
    async with cycle.factory() as db:
        assert (await load_execution(db, identity=cycle.identity)).kind is OutcomeKind.CONFLICT
        stale = await release_work(db, org_id=ORG, claim_id=claim.id, generation=5, reason=ReleaseReason.COMPLETED, terminal_evidence="old report")
        assert stale.reason == "stale_generation"
    cycle.identity = replace(cycle.identity, claim_generation=6)
    result = await tick(cycle)
    assert result.effects_succeeded == 1, (result, (await state(cycle))[0].block_detail)
    execution, claim, node, actions = await state(cycle)
    assert claim.active_run_id == cycle.calls[0]["message_id"] != cycle.root
    assert cycle.calls[0]["review_expect"]["author_run_id"] == cycle.root
    assert cycle.calls[0]["correlation"]["parent_principal"] == cycle.root + "#1"
    assert actions[0].detail["developer_recovery_decision_id"] == accepted["decision_id"]
    assert execution.handoff_receipt_ref is None and node.attempts == 1 and cycle.spend == Decimal(2)
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}") == raw
    assert (await call(cycle, req, accept=True))["created"] is False


async def test_held_completed_developer_recovers_once_without_releasing_lane(cycle):
    req, raw = await stranded(cycle, held=True)
    before = await state(cycle)
    preview = await call(cycle, req)
    assert preview["prior_claim_state"] == "held"
    assert preview["released_at"] is None and preview["release_reason"] is None
    assert preview["claim_generation"] == 6 and preview["prior_claim_generation"] == 5
    assert (await state(cycle))[1].active_run_id == cycle.root and not cycle.calls
    req.expected_snapshot = preview["snapshot"]
    results = await asyncio.gather(call(cycle, req, accept=True), call(cycle, req, accept=True))
    assert sorted(r["created"] for r in results) == [False, True]
    accepted = next(r for r in results if r["created"])
    execution, claim, node, actions = await state(cycle)
    assert claim.state == "held" and claim.generation == 6
    assert claim.active_run_id == claim.claim_event_id == reservation_id(accepted["decision_id"])
    assert claim.released_at is None and claim.release_reason is None
    assert execution.id == before[0].id and execution.handoff_receipt_ref is None
    assert execution.attempts == before[0].attempts and node.attempts == 1 and not actions
    async with cycle.factory() as db:
        assert (await load_execution(db, identity=cycle.identity)).kind is OutcomeKind.CONFLICT
    cycle.identity = replace(cycle.identity, claim_generation=6)
    assert (await tick(cycle)).effects_succeeded == 1
    execution, claim, node, actions = await state(cycle)
    assert len(actions) == len(cycle.calls) == 1 and claim.active_run_id == cycle.calls[0]["message_id"]
    assert cycle.calls[0]["review_expect"]["author_run_id"] == cycle.root
    assert execution.handoff_receipt_ref is None and node.attempts == 1 and cycle.spend == Decimal(2)
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}") == raw
    assert (await call(cycle, req, accept=True))["created"] is False


@pytest.mark.parametrize("change", ["active", "failed", "workload", "head", "owner", "event", "release", "pending_effect"])
async def test_held_recovery_refuses_changed_or_unverified_source(cycle, change):
    from src.orchestration.models import OrchestrationWorkClaim

    req, raw = await stranded(cycle, held=True)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    if change in {"active", "failed", "workload"}:
        if change == "workload":
            raw.pop("workload_binding")
        else:
            raw["terminal_outcome" if change == "failed" else "status"] = {"S": change}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    elif change == "head":
        cycle.head = "d" * 40
    else:
        async with cycle.factory() as db:
            claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
            if change == "owner":
                claim.active_run_id = "another-worker"
            elif change == "event":
                claim.claim_event_id = "another-event"
            elif change == "release":
                claim.state = "released"
                claim.active_run_id = None
                claim.release_reason = "completed"
                claim.released_at = datetime.now(UTC)
            else:
                db.add(
                    OrchestrationAction(org_id=ORG, execution_id=cycle.execution.id, operation_key="unknown", kind="provider-write", status="pending")
                )
            await db.commit()
    with pytest.raises(CycleBlockedError):
        await call(cycle, req, accept=True)
    execution, claim, node, _ = await state(cycle)
    assert claim.generation == 5 and node.attempts == 1 and not cycle.calls
    assert execution.handoff_receipt_ref is None
    async with cycle.factory() as db:
        assert not list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == KIND)))


@pytest.mark.parametrize("held", [False, True])
async def test_completed_block_notification_does_not_strand_delivered_developer(cycle, held):
    req, raw = await stranded(cycle, held=held)
    async with cycle.factory() as db:
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        execution.notification_receipt_ref = "notification-receipt"
        db.add(
            OrchestrationAction(
                org_id=ORG,
                execution_id=execution.id,
                operation_key="notify:block:dependency_unsatisfied:1:revision-1",
                kind="notify_execution_block",
                status="succeeded",
                receipt_ref="notification-receipt",
                observed_at=datetime.now(UTC),
            )
        )
        await db.commit()
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    cycle.identity = replace(cycle.identity, claim_generation=6)
    assert (await tick(cycle)).effects_succeeded == 1
    execution, claim, node, actions = await state(cycle)
    assert execution.notification_receipt_ref == "notification-receipt"
    assert execution.handoff_receipt_ref is None and node.attempts == 1 and cycle.spend == Decimal(2)
    async with cycle.factory() as db:
        notices = list(
            await db.scalars(
                select(OrchestrationAction).where(
                    OrchestrationAction.execution_id == execution.id, OrchestrationAction.kind == "notify_execution_block"
                )
            )
        )
    assert len(notices) == len(actions) == len(cycle.calls) == 1
    assert notices[0].status == "succeeded" and notices[0].receipt_ref == "notification-receipt"
    assert claim.active_run_id == cycle.calls[0]["message_id"]
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}") == raw


@pytest.mark.parametrize("change", ["pending", "unknown", "failed", "receipt", "empty_receipt", "observation", "business_effect"])
async def test_notification_exception_preserves_unresolved_effect_fence(cycle, change):
    req, _ = await stranded(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    async with cycle.factory() as db:
        db.add(
            OrchestrationAction(
                org_id=ORG,
                execution_id=cycle.execution.id,
                operation_key="notify:block:dependency_unsatisfied:1:revision-1",
                kind="provider-write" if change == "business_effect" else "notify_execution_block",
                status=change if change in {"pending", "unknown", "failed"} else "succeeded",
                receipt_ref=None if change == "receipt" else "" if change == "empty_receipt" else "notification-receipt",
                observed_at=None if change == "observation" else datetime.now(UTC),
            )
        )
        await db.commit()
    with pytest.raises(CycleBlockedError, match="pending_effect_requires_reconciliation"):
        await call(cycle, req, accept=True)
    execution, claim, node, actions = await state(cycle)
    assert claim.state == "released" and claim.generation == 5
    assert execution.handoff_receipt_ref is None and node.attempts == 1 and not cycle.calls
    assert not actions


@pytest.mark.parametrize(
    "change", ["head", "active", "revoked", "failed", "workload", "policy", "scope", "revision", "successor", "actor", "effect", "budget"]
)
async def test_accept_refuses_stale_or_unverified_delivery(cycle, change, monkeypatch):
    req, raw = await stranded(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    actor = "human"
    if change == "head":
        cycle.head = "d" * 40
    elif change in {"active", "revoked", "failed", "workload"}:
        if change == "workload":
            raw.pop("workload_binding")
        else:
            raw["terminal_outcome" if change == "failed" else "status"] = {"S": change}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    elif change == "actor":
        actor = "other"
        monkeypatch.setattr("src.orchestration.protected_developer_recovery.policy_owner_matches", AsyncMock(return_value=False))
    elif change == "budget":
        cycle.spend = Decimal(100)
    else:
        async with cycle.factory() as db:
            if change == "successor":
                await claim_work(db, binding=ClaimBinding(ORG, 123, 43), owner=ClaimOwner(OwnerKind.ENGINE_FLOW, cycle.flow.id), event_id="other-run")
            elif change == "policy":
                plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
                policy = dict(plan.plan_document["execution_policy"], policy_hash="c" * 64)
                plan.plan_document = {"execution_policy": policy}
            elif change == "effect":
                db.add(
                    OrchestrationAction(
                        org_id=ORG, execution_id=cycle.execution.id, operation_key="uncertain", kind="provider-write", status="pending"
                    )
                )
            else:
                binding = await db.get(OrchestrationPullRequestBinding, cycle.binding.id)
                if change == "scope":
                    binding.accepted_scope = "{}"
                else:
                    binding.revision += 1
            await db.commit()
    with pytest.raises(CycleBlockedError):
        await call(cycle, req, accept=True, actor_id=actor)
    async with cycle.factory() as db:
        assert not list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == KIND)))
    assert not cycle.calls and (await state(cycle))[2].attempts == 1


@pytest.mark.parametrize("failure", ["provision", "queue"])
async def test_partial_dispatch_replays_same_child_and_receipt(cycle, failure, monkeypatch):
    _, _, accepted = await accept(cycle)
    original = cycle.service.provision if failure == "provision" else cycle.service.queue.send_message
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "queue":
                original(*args, **kwargs)  # ambiguous response after queue acceptance
            raise RuntimeError("simulated boundary failure")
        return original(*args, **kwargs)

    if failure == "provision":
        monkeypatch.setattr(cycle.service, "provision", fail_once)
    else:
        monkeypatch.setattr(cycle.service.queue, "send_message", fail_once)
    await tick(cycle)
    execution, claim, node, actions = await state(cycle)
    assert len(actions) == 1 and claim.active_run_id != reservation_id(accepted["decision_id"])
    async with cycle.factory() as db:
        decision = await db.get(OrchestrationDecision, receipt_id(actions[0].operation_key))
        saved = json.loads(decision.reason)
    result = await tick(cycle)
    assert result.effects_succeeded == 1, (result, (await state(cycle))[0].block_detail)
    assert all(envelope == saved["envelope"] for envelope in cycle.calls)
    assert len((await state(cycle))[3]) == 1 and (await state(cycle))[1].generation == 6


@pytest.mark.parametrize("change", ["head", "revoked", "budget"])
async def test_acceptance_does_not_override_new_head_revocation_or_budget(cycle, change):
    _, raw, _ = await accept(cycle)
    if change == "head":
        cycle.head = "e" * 40
    elif change == "revoked":
        raw["status"] = {"S": "revoked"}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    else:
        cycle.spend = Decimal(100)
    assert (await tick(cycle)).effects_attempted == 0
    assert not cycle.calls and (await state(cycle))[0].handoff_receipt_ref is None


async def test_single_review_allowance_can_prepare_and_replay(cycle, monkeypatch):
    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        policy = cycle.policy.model_dump(mode="json")
        policy["limits"]["max_attempts_per_node"] = 1
        plan.plan_document = {"execution_policy": policy}
        await db.commit()
    await accept(cycle)
    original = cycle.service.provision
    monkeypatch.setattr(cycle.service, "provision", lambda *args: (_ for _ in ()).throw(RuntimeError("not provisioned")))
    await tick(cycle)
    monkeypatch.setattr(cycle.service, "provision", original)
    result = await tick(cycle)
    assert result.effects_succeeded == 1, (result, (await state(cycle))[0].block_detail)
    assert cycle.calls[0]["review_cycle_input"]["remaining_attempts"] == 1


@pytest.mark.parametrize("change", ["budget", "revoked_parent", "claim", "binding"])
async def test_unprovisioned_recovery_replay_rechecks_current_authority(cycle, monkeypatch, change):
    _, raw, _ = await accept(cycle)
    original = cycle.service.provision
    monkeypatch.setattr(cycle.service, "provision", lambda *args: (_ for _ in ()).throw(RuntimeError("not provisioned")))
    await tick(cycle)
    before = await state(cycle)
    assert len(before[3]) == 1 and not cycle.calls
    assert await cycle.service.protected(ORG, before[1].active_run_id) is None
    if change == "budget":
        cycle.spend = Decimal(100)
    elif change == "revoked_parent":
        raw["status"] = {"S": "revoked"}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    else:
        async with cycle.factory() as db:
            if change == "claim":
                claim = await db.get(OrchestrationWorkClaim, before[1].id)
                claim.generation += 1
            else:
                binding = await db.get(OrchestrationPullRequestBinding, cycle.binding.id)
                binding.revision += 1
            await db.commit()
    monkeypatch.setattr(cycle.service, "provision", original)
    result = await tick(cycle)
    after = await state(cycle)
    assert result.effects_succeeded == 0 and not cycle.calls
    assert len(after[3]) == 1 and after[3][0].operation_key == before[3][0].operation_key
    assert after[2].attempts == before[2].attempts


async def test_changed_request_cannot_replay_accepted_snapshot(cycle):
    req, _, _ = await accept(cycle)
    req.expected_head_sha = "e" * 40
    with pytest.raises(CycleBlockedError, match="recovery_receipt_conflict"):
        await call(cycle, req, accept=True)


async def test_late_policy_change_rolls_back_dispatch_transfer(cycle):
    await accept(cycle)

    async def change_policy(*args, **kwargs):
        async with cycle.factory() as db:
            plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
            policy = dict(plan.plan_document["execution_policy"], policy_hash="d" * 64)
            plan.plan_document = {"execution_policy": policy}
            await db.commit()
        return {"model_resolved": "openai.gpt-6-sol"}

    cycle.launch.side_effect = change_policy
    await tick(cycle)
    execution, claim, _, actions = await state(cycle)
    assert claim.active_run_id.startswith("recovery:") and not cycle.calls
    async with cycle.factory() as db:
        assert await db.get(OrchestrationDecision, receipt_id(actions[0].operation_key)) is None


@pytest.mark.parametrize("held", [False, True])
async def test_failed_generation_adoption_rolls_back_entire_acceptance(cycle, monkeypatch, held):
    req, _ = await stranded(cycle, held=held)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    monkeypatch.setattr(
        "src.orchestration.protected_developer_recovery.advance_execution", AsyncMock(return_value=SimpleNamespace(kind=OutcomeKind.CONFLICT))
    )
    with pytest.raises(CycleBlockedError, match="recovery_assignment_changed"):
        await call(cycle, req, accept=True)
    execution, claim, _, _ = await state(cycle)
    assert execution.claim_generation == claim.generation == 5 and claim.state == ("held" if held else "released")
    assert claim.claim_event_id == cycle.root and claim.active_run_id == (cycle.root if held else None)
    async with cycle.factory() as db:
        assert not list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == KIND)))


async def amend_to_v3(ctx, monkeypatch, change=None):
    from src.orchestration import review_cycle_dispatch
    from src.orchestration.continuation import digest

    async with ctx.factory() as db:
        base = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = {
            **base.plan_document,
            "nodes": [{"address": "cycle/E1/W1/N1", "kind": "story", "issue_ref": "43", "title": "Bound story"}],
            "edges": [],
        }
        base.plan_document = document
        base.accepted_by_decision_id = ctx.approval.id
        for version in range(ctx.plan.version + 1, 4):
            base.superseded_at = datetime.now(UTC)
            amendment = OrchestrationDecision(
                org_id=ORG,
                flow_id=ctx.flow.id,
                kind="plan_amended",
                actor_kind="service" if change == "actor" and version == 3 else "human",
                actor_id="human",
                actor_role="owner",
            )
            db.add(amendment)
            await db.flush()
            updated = json.loads(json.dumps(document))
            updated["execution_policy"].update(policy_id=f"policy-v{version}", policy_hash=str(version) * 64)
            updated["nodes"].append({"address": f"cycle/E1/W{version}/G1", "kind": "gate"})
            if version == 3 and change == "definition":
                updated["nodes"][0]["criteria"] = ["new requirement"]
            if version == 3 and change == "prerequisite":
                updated["edges"] = [{"from_address": "cycle/E1/W3/G1", "to_address": "cycle/E1/W1/N1"}]
            base = OrchestrationAcceptedPlan(
                org_id=ORG,
                flow_id=ctx.flow.id,
                version=version,
                plan_document=updated,
                plan_hash=digest(updated),
                accepted_by_decision_id=amendment.id,
            )
            db.add(base)
            document = updated
        await db.commit()
    original = review_cycle_dispatch.resolve_authorization_context

    async def authorization(db, **kwargs):
        result = await original(db, **kwargs)
        return replace(result, accepted_plan_version=kwargs["plan_version"], in_force_plan_version=kwargs["plan_version"])

    monkeypatch.setattr(review_cycle_dispatch, "resolve_authorization_context", authorization)


async def test_first_review_under_v3_preserves_earlier_assignment(cycle, monkeypatch):
    req, raw = await stranded(cycle)
    await amend_to_v3(cycle, monkeypatch)
    req.expected_plan_version = 3
    preview = await call(cycle, req)
    assert preview["accepted_plan_version"] == 2 and preview["plan_version"] == 3
    assert preview["policy_id"] == "policy-v3" and preview["policy_hash"] == "3" * 64
    req.expected_snapshot = preview["snapshot"]
    await call(cycle, req, accept=True)
    cycle.identity = replace(cycle.identity, claim_generation=6)
    result = await tick(cycle)
    assert result.effects_succeeded == 1, (result, (await state(cycle))[0].block_detail)
    assert (await state(cycle))[0].accepted_plan_version == 2
    assert cycle.calls[0]["review_expect"]["accepted_plan_version"] == 2
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}") == raw


@pytest.mark.parametrize("change", ["definition", "prerequisite", "actor"])
async def test_first_review_refuses_nonpreserving_or_nonhuman_amendment(cycle, monkeypatch, change):
    req, _ = await stranded(cycle)
    await amend_to_v3(cycle, monkeypatch, change)
    req.expected_plan_version = 3
    with pytest.raises(CycleBlockedError):
        await call(cycle, req)
    assert (await state(cycle))[1].state == "released" and not cycle.calls


async def test_existing_entrypoint_selects_developer_protocol(cycle, monkeypatch):
    from src.orchestration.review_recovery import request_review_recovery

    req, _ = await stranded(cycle)
    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.get_engine_authority_writer", lambda: cycle.service.writer)
    async with cycle.factory() as db:
        preview = await request_review_recovery(db, org_id=ORG, node_id=cycle.node.id, actor_id="human", actor_role="owner", request=req)
    assert preview["contract"] == "protected-developer-review-recovery/v1"


async def test_recovery_reservation_cannot_authorize_another_stage(cycle):
    from src.orchestration.execution_policy import Action
    from src.orchestration.execution_runner import RunnerContext

    _, _, accepted = await accept(cycle)
    async with cycle.factory() as db:
        loaded = await load_execution(db, identity=cycle.identity)
        with pytest.raises(CycleBlockedError, match="recovery_action_not_authorized"):
            await cycle.service.authorize(
                db,
                RunnerContext(cycle.identity, loaded.record, datetime.now(UTC)),
                cycle.node,
                cycle.binding,
                reservation_id(accepted["decision_id"]),
                Action.MERGE,
                recovery_decision_id=accepted["decision_id"],
            )


async def test_postprovision_policy_change_blocks_queue_publication(cycle, monkeypatch):
    await accept(cycle)
    original = cycle.service.provision
    loop = asyncio.get_running_loop()

    async def change_policy():
        async with cycle.factory() as db:
            plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
            plan.plan_document = {"execution_policy": dict(plan.plan_document["execution_policy"], policy_hash="e" * 64)}
            await db.commit()

    def provision_then_amend(*args):
        result = original(*args)
        asyncio.run_coroutine_threadsafe(change_policy(), loop).result(timeout=10)
        return result

    monkeypatch.setattr(cycle.service, "provision", provision_then_amend)
    result = await tick(cycle)
    assert result.effects_succeeded == 0 and not cycle.calls
    assert not (await state(cycle))[1].active_run_id.startswith("recovery:")
