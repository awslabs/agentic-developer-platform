"""Real PostgreSQL/protected-store reproduction of #8142, including safe replay."""

# ruff: noqa: F811

import asyncio
import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.agentauth.bootstrap import BootstrapRefusedError
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationWorkClaim
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.review_cycle_dispatch import receipt_id, validate_continuation_assignment
from src.orchestration.review_recovery import ReviewRecoveryRequest
from src.orchestration.work_claims import Disposition, ReleaseReason, release_work
from tests.orchestration.test_retained_work_recovery import call, failed_work
from tests.orchestration.test_review_cycle import ORG, cycle, pg_server, pg_url, review, state, store, tick  # noqa: F401

pytestmark = pytest.mark.parametrize("cycle", [{"missing_handoff": True, "node_attempt": 3, "max_attempts": 3}], indirect=True)


async def failed_reviewer(ctx, monkeypatch, *, released=True, outcome="failed"):
    request, originals = await failed_work(ctx, monkeypatch)
    # Match the fixture's canonical human identity for source-owner revalidation.
    monkeypatch.setattr("src.shared.identity.resolver.resolve_root_user_entity_id", AsyncMock(return_value="human"))
    monkeypatch.setattr(
        "src.orchestration.released_reviewer_recovery.require_budget",
        AsyncMock(
            return_value={
                "chain_cap_usd": "500",
                "run_cap_usd": "100",
                "flow_cap_usd": "500",
            }
        ),
    )
    request.expected_snapshot = (await call(ctx, request))["snapshot"]
    await call(ctx, request, accept=True)
    ctx.identity = replace(ctx.identity, claim_generation=6)
    assert (await tick(ctx)).effects_succeeded == 1
    await review(
        ctx,
        approve=outcome == "complete",
        findings=[] if outcome == "complete" else [{"id": "R1", "summary": "Retained reviewer found an unresolved boundary defect."}],
    )
    _, claim, _, _ = await state(ctx)
    raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{claim.active_run_id}")
    raw.update(terminal_outcome={"S": outcome}, workload_binding={"S": "exited-reviewer"})
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    ctx.failed_reviewer = claim.active_run_id
    if released:
        # Reproduce the historical pre-fix release in the isolated SQL fixture.
        # Production recovery never writes around release_work/claim_work.
        async with ctx.factory() as db:
            row = await db.get(OrchestrationWorkClaim, claim.id)
            row.state, row.release_reason = "released", "failed"
            row.released_at, row.active_run_id, row.lease_expires_at = datetime.now(UTC), None, None
            await db.commit()
    return ReviewRecoveryRequest(
        mode="review_released_reviewer",
        expected_attempt=3,
        expected_plan_version=ctx.plan.version,
        expected_run_id=ctx.failed_reviewer,
        expected_head_sha=ctx.head,
        reason="Owner resumes the exact released retained reviewer without another developer attempt.",
    ), dict(originals, **{ctx.failed_reviewer: raw})


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("reason", [ReleaseReason.FAILED, ReleaseReason.COMPLETED])
async def test_terminal_reviewer_release_is_retained_without_handoff(cycle, monkeypatch, pending, reason):
    await failed_reviewer(cycle, monkeypatch, released=False, outcome="complete" if reason is ReleaseReason.COMPLETED else "failed")
    before = await state(cycle)
    async with cycle.factory() as db:
        if pending:
            db.add(
                OrchestrationAction(
                    org_id=ORG,
                    execution_id=before[0].id,
                    operation_key="uncommitted-next-review",
                    kind="review_cycle_dispatch",
                    status="prepared",
                    detail={},
                )
            )
            await db.flush()
        result = await release_work(
            db, org_id=ORG, claim_id=before[1].id, generation=6, reason=reason, terminal_evidence=f"protected {reason.value} reviewer"
        )
        await db.commit()
    assert result.disposition is Disposition.BLOCKED and result.reason == "continuation_outstanding"
    after = await state(cycle)
    assert after[1].state == "held" and after[1].active_run_id == cycle.failed_reviewer
    assert after[0].handoff_receipt_ref is None and after[2].attempts == 3


async def test_failed_developer_still_releases_nonterminal_missing_handoff(cycle):
    async with cycle.factory() as db:
        result = await release_work(
            db, org_id=ORG, claim_id=cycle.claim.id, generation=5, reason=ReleaseReason.FAILED, terminal_evidence="protected failed developer"
        )
        await db.commit()
    assert result.disposition is Disposition.ADMITTED
    assert (await state(cycle))[1].state == "released"


async def test_exact_recovery_keeps_attempts_findings_spend_chain_and_dispatches_one_sibling(cycle, monkeypatch):
    request, protected = await failed_reviewer(cycle, monkeypatch)
    before = await state(cycle)
    reconcile = AsyncMock(side_effect=AssertionError("Preview must not reconcile or seed accounting"))
    with monkeypatch.context() as preview_patch:
        preview_patch.setattr("src.orchestration.flow_meter.reconcile_flow_meter", reconcile)
        preview = await call(cycle, request)
        request.expected_snapshot = preview["snapshot"]
        accepted = await asyncio.gather(call(cycle, request, accept=True), call(cycle, request, accept=True))
    assert sorted(r["created"] for r in accepted) == [False, True]
    reconcile.assert_not_called()
    execution, claim, node, actions = await state(cycle)
    assert node.attempts == 3 and execution.cycle == 3 and execution.attempts == before[0].attempts
    assert execution.id == before[0].id and claim.id == before[1].id and claim.generation == 7
    assert execution.handoff_receipt_ref is None and len(actions) == 1
    cycle.identity = replace(cycle.identity, claim_generation=7)
    result = await tick(cycle)
    assert result.effects_succeeded == 1, (result, (await state(cycle))[0].block_detail)
    assert len(cycle.calls) == 2
    old, new = cycle.calls
    assert new["persona"] == "agent-codex-reviewer" and new["message_id"] != old["message_id"]
    assert new["correlation"] == old["correlation"]
    assert new["review_expect"]["claim_generation"] == 7
    assert new["review_expect"]["author_run_id"] == old["review_expect"]["author_run_id"]
    assert new["review_cycle_input"]["findings"][0]["id"] == "R1"
    assert new["review_cycle_input"]["allow_story_repairs"] is False
    assert cycle.spend == 2
    assert {run: cycle.store._read(f"TENANT#{ORG}", f"EXEC#{run}") for run in protected} == protected
    assert (await call(cycle, request, accept=True))["created"] is False


@pytest.mark.parametrize("failure", ["provision", "queue"])
async def test_lost_ack_replays_same_successor_and_generation(cycle, monkeypatch, failure):
    request, _ = await failed_reviewer(cycle, monkeypatch)
    request.expected_snapshot = (await call(cycle, request))["snapshot"]
    await call(cycle, request, accept=True)
    cycle.identity = replace(cycle.identity, claim_generation=7)
    target, name = (cycle.service, "provision") if failure == "provision" else (cycle.service.queue, "send_message")
    original = getattr(target, name)
    tried = []

    def uncertain(*args, **kwargs):
        if not tried:
            tried.append(True)
            original(*args, **kwargs)  # Effect happened; acknowledgement was lost.
            raise RuntimeError("offline lost acknowledgement")
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, uncertain)
    await tick(cycle)
    first = await state(cycle)
    await tick(cycle)
    second = await state(cycle)
    assert len(second[3]) == 2 and second[3][-1].operation_key == first[3][-1].operation_key
    assert second[1].generation == 7 and second[2].attempts == 3
    assert len({call["message_id"] for call in cycle.calls}) == 2


async def test_normal_validator_still_refuses_historical_reviewer(cycle, monkeypatch):
    _, protected = await failed_reviewer(cycle, monkeypatch)
    grant = cycle.store.live_grant(invocation_id=cycle.failed_reviewer, tenant_id=ORG, attempt=1, now=datetime.now(UTC))
    async with cycle.factory() as db:
        with pytest.raises(BootstrapRefusedError):
            await validate_continuation_assignment(db, execution=protected[cycle.failed_reviewer], grant=grant, node=cycle.node)


@pytest.mark.parametrize(
    "change",
    [
        "head",
        "budget",
        "owner",
        "terminal",
        "workload",
        "raw_tenant",
        "raw_node",
        "raw_run",
        "raw_parent",
        "raw_receipt",
        "claim_owner",
        "claim_generation",
        "claim_issue",
        "claim_event",
        "claim_release",
        "claim_time",
        "receipt_actor",
        "receipt_tenant",
        "receipt_execution",
        "receipt_generation",
        "receipt_claim",
        "receipt_action",
        "receipt_raw",
        "receipt_list",
        "receipt_nested",
        "source_receipt",
        "source_terminal",
        "findings",
        "pending_effect",
        "newer_dispatch",
        "retired_dispatch",
        "grant",
        "policy",
    ],
)
async def test_changed_snapshot_cannot_reserve_claim_or_dispatch(cycle, monkeypatch, change):
    from src.orchestration.models import OrchestrationAcceptedPlan

    request, _ = await failed_reviewer(cycle, monkeypatch)
    request.expected_snapshot = (await call(cycle, request))["snapshot"]
    execution, claim, node, actions = await state(cycle)
    if change == "head":
        cycle.head = "d" * 40
    elif change == "budget":
        monkeypatch.setattr(
            "src.orchestration.released_reviewer_recovery.require_budget", AsyncMock(side_effect=CycleBlockedError("retained_budget_exhausted"))
        )
    elif change == "owner":
        monkeypatch.setattr("src.orchestration.protected_developer_recovery.policy_owner_matches", AsyncMock(return_value=False))
    elif change in {"terminal", "workload", "raw_tenant", "raw_node", "raw_run", "raw_parent", "raw_receipt", "source_terminal"}:
        run = cycle.source if change == "source_terminal" else cycle.failed_reviewer
        raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{run}")
        key, value = {
            "terminal": ("terminal_outcome", "complete"),
            "workload": ("workload_binding", "changed-workload"),
            "raw_tenant": ("tenant_id", "other-tenant"),
            "raw_node": ("orchestration_node_id", "other-node"),
            "raw_run": ("invocation_id", "other-run"),
            "raw_parent": ("parent_principal", "other-parent#1"),
            "raw_receipt": ("orchestration_continuation_receipt", "other-receipt"),
            "source_terminal": ("terminal_outcome", "complete"),
        }[change]
        raw[key] = {"S": value}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    elif change == "grant":
        raw = cycle.store._read(f"TENANT#{ORG}", f"GRANT#{cycle.failed_reviewer}#1")
        raw["revoked"] = {"BOOL": True}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    else:
        async with cycle.factory() as db:
            if change.startswith("claim_"):
                row = await db.get(OrchestrationWorkClaim, claim.id)
                key, value = {
                    "claim_owner": ("owner_ref", "other-flow"),
                    "claim_generation": ("generation", 8),
                    "claim_issue": ("issue_number", 999),
                    "claim_event": ("claim_event_id", "other-run"),
                    "claim_release": ("release_reason", "completed"),
                    "claim_time": ("released_at", datetime.now(UTC) + timedelta(seconds=1)),
                }[change]
                setattr(row, key, value)
            elif change.startswith("receipt_") or change == "source_receipt":
                row = await db.get(OrchestrationDecision, receipt_id(actions[-1].operation_key))
                # Isolated corruption fixture. Production decisions are append-only;
                # recovery must also refuse malformed imported/historical rows.
                db.expunge(row)
                data = json.loads(row.reason)
                if change == "receipt_actor":
                    row.actor_id = "untrusted-producer"
                elif change == "receipt_tenant":
                    row.org_id = "other-tenant"
                elif change == "source_receipt":
                    source = await db.get(OrchestrationDecision, data["developer_recovery_decision_id"])
                    await db.execute(
                        update(OrchestrationDecision.__table__)
                        .where(OrchestrationDecision.__table__.c.id == source.id)
                        .values(actor_id="untrusted-source-owner")
                    )
                elif change == "receipt_raw":
                    row.reason = "not-json"
                elif change == "receipt_list":
                    row.reason = "[]"
                else:
                    if change == "receipt_execution":
                        data["execution_id"] = "other-execution"
                    elif change == "receipt_generation":
                        data["claim_generation"] = 99
                    elif change == "receipt_claim":
                        data["envelope"]["review_expect"]["claim_id"] = "other-claim"
                    elif change == "receipt_action":
                        data["action"] = "develop"
                    elif change == "receipt_nested":
                        data["envelope"] = []
                    row.reason = json.dumps(data)
                await db.execute(
                    update(OrchestrationDecision.__table__)
                    .where(OrchestrationDecision.__table__.c.id == row.id)
                    .values(actor_id=row.actor_id, org_id=row.org_id, reason=row.reason)
                )
            elif change == "policy":
                plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
                data = copy.deepcopy(plan.plan_document)
                data["execution_policy"]["policy_hash"] = "e" * 64
                plan.plan_document = data
            elif change == "findings":
                evidence = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "review_evidence"))
                evidence.detail = dict(evidence.detail, complete_review="true")
            else:
                dispatch = change != "pending_effect"
                action = OrchestrationAction(
                    org_id=ORG,
                    execution_id=execution.id,
                    operation_key="later-effect",
                    kind="review_cycle_dispatch" if dispatch else "provider-write",
                    status="failed" if change == "retired_dispatch" else "succeeded" if dispatch else "unknown",
                    detail={},
                )
                db.add(action)
                await db.flush()
                if change == "retired_dispatch":
                    action.receipt_ref = "unstarted-review-head-change:" + action.id
            await db.commit()
    with pytest.raises(CycleBlockedError):
        await call(cycle, request, accept=True)
    after = await state(cycle)
    assert after[1].state == "released" and after[2].attempts == 3 and len(cycle.calls) == 1


def move_clock(monkeypatch):
    from src.orchestration import released_reviewer_recovery

    current = released_reviewer_recovery.datetime

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.now(tz or UTC) + timedelta(seconds=901)

    monkeypatch.setattr(released_reviewer_recovery, "datetime", Later)


async def accepted_reviewer(cycle, monkeypatch):
    request, _ = await failed_reviewer(cycle, monkeypatch)
    request.expected_snapshot = (await call(cycle, request))["snapshot"]
    accepted = await call(cycle, request, accept=True)
    cycle.identity = replace(cycle.identity, claim_generation=7)
    request.renew_decision_id, request.expected_snapshot = accepted["decision_id"], None
    return request


@pytest.mark.parametrize("pending", [False, True])
async def test_finite_renewal_preserves_old_history_and_reuses_unstarted_intent(cycle, monkeypatch, pending):
    request = await accepted_reviewer(cycle, monkeypatch)
    async with cycle.factory() as db:
        original = (await db.get(OrchestrationDecision, request.renew_decision_id)).reason
    if pending:
        with monkeypatch.context() as unstarted:
            unstarted.setattr(cycle.service, "prepare", AsyncMock(side_effect=RuntimeError("offline before preparation")))
            await tick(cycle)
    move_clock(monkeypatch)
    await tick(cycle)
    before = await state(cycle)
    with monkeypatch.context() as preview_patch:
        reconcile = AsyncMock(side_effect=AssertionError("Renewal preview must not reconcile accounting"))
        preview_patch.setattr("src.orchestration.flow_meter.reconcile_flow_meter", reconcile)
        request.expected_snapshot = (await call(cycle, request))["snapshot"]
        results = await asyncio.gather(call(cycle, request, accept=True), call(cycle, request, accept=True))
    assert sorted(r["created"] for r in results) == [False, True]
    after = await state(cycle)
    assert after[1].generation == 7 and after[0].attempts == before[0].attempts and after[2].attempts == 3
    assert [a.operation_key for a in after[3]] == [a.operation_key for a in before[3]]
    result = await tick(cycle)
    assert result.effects_succeeded == 1, (result, (await state(cycle))[0].block_detail)
    assert len(cycle.calls) == 2
    async with cycle.factory() as db:
        assert (await db.get(OrchestrationDecision, request.renew_decision_id)).reason == original


@pytest.mark.parametrize(
    "kind,status,key,receipt",
    [
        ("notify_execution_block", "unknown", "notify:block:uncertain", "notice"),
        ("provider-write", "succeeded", "notify:block:unrelated", "notice"),
        ("notify_execution_block", "succeeded", "unrecognized-operation", "notice"),
        ("notify_execution_block", "succeeded", "notify:block:no-receipt", None),
    ],
)
async def test_renewal_notification_exception_does_not_hide_other_effects(cycle, monkeypatch, kind, status, key, receipt):
    request = await accepted_reviewer(cycle, monkeypatch)
    async with cycle.factory() as db:
        db.add(
            OrchestrationAction(
                org_id=ORG, execution_id=cycle.execution.id, operation_key=key, kind=kind, status=status, receipt_ref=receipt, detail={}
            )
        )
        await db.commit()
    move_clock(monkeypatch)
    with pytest.raises(CycleBlockedError, match="pending_effect_requires_reconciliation"):
        await call(cycle, request)
    assert len(cycle.calls) == 1 and (await state(cycle))[1].generation == 7


@pytest.mark.parametrize("boundary", ["receipt", "provision", "queue", "claim"])
async def test_expired_potentially_published_dispatch_requires_reconciliation(cycle, monkeypatch, boundary):
    request = await accepted_reviewer(cycle, monkeypatch)
    if boundary == "claim":
        async with cycle.factory() as db:
            claim = await db.get(OrchestrationWorkClaim, cycle.identity.claim_id)
            claim.active_run_id = claim.claim_event_id = "other-owner"
            await db.commit()
    else:
        target, name = (cycle.service, "provision") if boundary == "receipt" else (cycle.service.queue, "send_message")
        original = getattr(target, name)

        def uncertain(*args, **kwargs):
            if boundary == "queue":
                original(*args, **kwargs)
            raise RuntimeError("offline uncertain dispatch")

        monkeypatch.setattr(target, name, uncertain)
        await tick(cycle)
    move_clock(monkeypatch)
    with pytest.raises(CycleBlockedError, match="released_reviewer_renewal_requires_reconciliation"):
        await call(cycle, request)
    assert (await state(cycle))[1].generation == 7 and (await state(cycle))[2].attempts == 3


@pytest.mark.parametrize("boundary", ["transfer", "provision"])
@pytest.mark.parametrize("change", ["head", "budget", "source", "expiry"])
async def test_dispatch_rechecks_authority_at_effect_boundaries(cycle, monkeypatch, boundary, change):
    from src.orchestration import released_reviewer_recovery, work_claims

    await accepted_reviewer(cycle, monkeypatch)

    def drift():
        if change == "head":
            cycle.head = "d" * 40
        elif change == "budget":
            monkeypatch.setattr(released_reviewer_recovery, "require_budget", AsyncMock(side_effect=CycleBlockedError("retained_budget_exhausted")))
        elif change == "expiry":
            move_clock(monkeypatch)
        else:
            raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.source}")
            raw["terminal_outcome"] = {"S": "complete"}
            cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)

    if boundary == "transfer":
        original = work_claims.continue_run

        async def race(*args, **kwargs):
            drift()
            return await original(*args, **kwargs)

        monkeypatch.setattr(work_claims, "continue_run", race)
    else:
        original = cycle.service.provision

        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            drift()
            return result

        monkeypatch.setattr(cycle.service, "provision", race)
    assert (await tick(cycle)).effects_succeeded == 0
    assert len(cycle.calls) == 1 and (await state(cycle))[2].attempts == 3
