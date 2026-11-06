"""An owner can stop a verified failed review without licensing a retry or success."""

# ruff: noqa: F811

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select, update

from src.orchestration import failed_review_settlement as settlement
from src.orchestration.failed_review_settlement import KIND, request_settlement
from src.orchestration.merge_provider import MergeState
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.review_recovery import request_review_recovery
from src.orchestration.shared_amendment import quiescence_failures
from src.orchestration.work_claims import ClaimReceipt, Disposition
from tests.orchestration.test_protected_review_recovery import failed
from tests.orchestration.test_review_cycle import ORG, cycle, pg_server, pg_url, state, store, tick  # noqa: F401


async def prepare(ctx):
    req, raw = await failed(ctx)
    raw.update(
        workload_binding={"S": "pod:failed-reviewer"},
        terminal_report_digest={"S": "e" * 64},
        terminal_recorded_at={"S": datetime.now(UTC).isoformat()},
    )
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    req.mode = "settle_failed"
    req.expected_attempt = ctx.node.attempts
    return req, raw


def provider(ctx, **changes):
    remote = MergeState(ctx.head, "b" * 40, "work", "main", False, True, None, None, None, datetime.now(UTC).isoformat())
    return SimpleNamespace(read=AsyncMock(return_value=replace(remote, **changes)))


async def call(ctx, req, *, accept=False, actor_id="human", actor_role="owner", remote=None):
    async with ctx.factory() as db:
        result = await request_settlement(
            db,
            org_id=ORG,
            node_id=ctx.node.id,
            actor_id=actor_id,
            actor_role=actor_role,
            request=req,
            accept=accept,
            services=ctx.service,
            provider=remote or provider(ctx),
        )
        if accept:
            await db.commit()
        return result


async def histories(ctx):
    async with ctx.factory() as db:
        actions = list(await db.scalars(select(OrchestrationAction).order_by(OrchestrationAction.id)))
        decisions = list(await db.scalars(select(OrchestrationDecision).order_by(OrchestrationDecision.id)))
        binding = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
        return ([settlement.action_snapshot(a) for a in actions], [(d.id, d.kind, d.reason) for d in decisions], binding.head_sha, binding.revision)


async def quiescence(ctx):
    execution, claim, node, _ = await state(ctx)
    async with ctx.factory() as db:
        return quiescence_failures(
            dict(
                nodes=[node],
                executions=list(await db.scalars(select(OrchestrationExecution))),
                claims=[claim],
                actions=list(await db.scalars(select(OrchestrationAction))),
                reports=[],
                authoring=[],
            )
        )


async def test_preview_is_inert_and_accept_concludes_without_retry_or_success(cycle):
    req, raw = await prepare(cycle)
    before, claim_before, node_before, _ = await state(cycle)
    history = await histories(cycle)
    assert await quiescence(cycle) == ["active_nodes_cross_plan_boundary", "unfinished_execution", "unsettled_work_claim"]
    preview = await call(cycle, req)
    assert preview["next_action"] == "record_failed_node_only"
    assert await histories(cycle) == history
    assert (await state(cycle))[0].revision == before.revision
    req.expected_snapshot = preview["snapshot"]
    result = await call(cycle, req, accept=True)
    assert result["created"] is True
    after, claim_after, node_after, _ = await state(cycle)
    assert (after.status, after.phase, after.next_check_at, after.pending_action_key) == ("concluded", "concluded", None, None)
    assert after.id == before.id and after.attempts == before.attempts
    assert after.handoff_receipt_ref == before.handoff_receipt_ref
    assert (claim_after.state, claim_after.release_reason, claim_after.active_run_id) == ("released", "failed", None)
    assert claim_after.id == claim_before.id and claim_after.generation == claim_before.generation
    assert node_after.state == "failed" and node_after.attempts == node_before.attempts
    remaining = await histories(cycle)
    assert remaining[0] == history[0] and remaining[2:] == history[2:]
    new = [d for d in remaining[1] if d[0] not in {d[0] for d in history[1]}]
    audits = [d for d in new if d[1] == KIND]
    assert len(audits) == 1
    assert json.loads(audits[0][2])["request"] == req.model_dump(mode="json")
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{req.expected_run_id}") == raw
    assert await quiescence(cycle) == []
    assert (await call(cycle, req, accept=True))["created"] is False
    assert (await tick(cycle, force=False)).effects_attempted == 0
    assert len(cycle.calls) == 1 and await histories(cycle) == remaining


async def test_settlement_ignores_expired_budgets_and_attempt_allowances(cycle, monkeypatch):
    req, _ = await prepare(cycle)
    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["expires_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        plan.plan_document = document
        await db.execute(update(OrchestrationExecution).values(attempts=150))
        await db.commit()
    cycle.spend = Decimal("5000")
    monkeypatch.setattr(cycle.service, "authorize", AsyncMock(side_effect=AssertionError("stopping work must not authorize new work")))
    monkeypatch.setattr(cycle.store, "live_grant", Mock(side_effect=AssertionError("expired grant must not prevent recording terminal failure")))
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    assert (await state(cycle))[0].attempts == 150


async def test_current_pr_head_can_differ_from_failed_review_without_endorsing_it(cycle):
    req, _ = await prepare(cycle)
    historical_head = cycle.binding.head_sha
    cycle.head = req.expected_head_sha = "c" * 40
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    result = await call(cycle, req, accept=True)
    assert result["head_sha"] == cycle.head and result["binding_head_sha"] == historical_head
    assert (await histories(cycle))[2] == historical_head


@pytest.mark.parametrize(
    "change", ["active", "successful", "missing_digest", "missing_workload", "bad_seal", "foreign_metadata", "future", "malformed"]
)
async def test_unverified_protected_exit_is_refused(cycle, change):
    req, raw = await prepare(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    if change == "active":
        raw["status"] = {"S": "active"}
    elif change == "successful":
        raw["terminal_outcome"] = {"S": "complete"}
    elif change == "missing_digest":
        raw.pop("terminal_report_digest")
    elif change == "missing_workload":
        raw.pop("workload_binding")
    elif change == "bad_seal":
        raw["envelope_digest"] = {"S": "f" * 64}
    elif change == "foreign_metadata":
        raw["orchestration_node_id"] = {"S": "another-node"}
    elif change == "future":
        raw["terminal_recorded_at"] = {"S": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
    elif change == "malformed":
        raw["terminal_report_digest"] = {"N": "42"}
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    with pytest.raises(CycleBlockedError, match="failed_reviewer_terminal_unverified"):
        await call(cycle, req, accept=True)
    assert (await state(cycle))[1].state == "held"


@pytest.mark.parametrize("changes", [{"merged": True}, {"open": False}, {"queue_id": "queue:1"}, {"head_sha": "d" * 40}])
async def test_changed_or_merged_pr_is_refused(cycle, changes):
    req, _ = await prepare(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    with pytest.raises(CycleBlockedError, match="current_pr_changed"):
        await call(cycle, req, accept=True, remote=provider(cycle, **changes))
    assert (await state(cycle))[1].state == "held"


@pytest.mark.parametrize(
    "change", ["plan", "attempt", "run", "actor", "role", "claim", "claim_event", "binding", "pending", "unknown", "successor", "delivery"]
)
async def test_assignment_and_effect_changes_are_refused(cycle, change, monkeypatch):
    req, _ = await prepare(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    kwargs = {}
    if change == "plan":
        req.expected_plan_version += 1
    elif change == "attempt":
        req.expected_attempt += 1
    elif change == "run":
        req.expected_run_id = "another-run"
    elif change == "actor":
        kwargs["actor_id"] = "other-human"
        monkeypatch.setattr(settlement, "policy_owner_matches", AsyncMock(return_value=False))
    elif change == "role":
        kwargs["actor_role"] = "developer"
    else:
        async with cycle.factory() as db:
            if change == "claim":
                await db.execute(update(OrchestrationWorkClaim).values(generation=cycle.identity.claim_generation + 1))
            elif change == "claim_event":
                await db.execute(update(OrchestrationWorkClaim).values(claim_event_id="previous-worker"))
            elif change == "binding":
                await db.execute(update(OrchestrationPullRequestBinding).values(revision=2))
            elif change == "pending":
                await db.execute(update(OrchestrationExecution).values(pending_action_key="pending"))
            else:
                db.add(
                    OrchestrationAction(
                        org_id=ORG,
                        execution_id=cycle.execution.id,
                        operation_key="later",
                        kind={"unknown": "review_cycle_dispatch", "successor": "review_cycle_dispatch", "delivery": "merge_pull_request"}[change],
                        status="unknown" if change == "unknown" else "succeeded",
                        detail={},
                    )
                )
            await db.commit()
    with pytest.raises(CycleBlockedError):
        await call(cycle, req, accept=True, **kwargs)
    assert (await state(cycle))[1].state == "held"


async def test_accept_rechecks_revision_after_provider_read(cycle):
    req, _ = await prepare(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    remote = provider(cycle)
    value = remote.read.return_value

    async def racing_read(binding):
        async with cycle.factory() as db:
            await db.execute(update(OrchestrationExecution).values(revision=OrchestrationExecution.revision + 1))
            await db.commit()
        return value

    remote.read.side_effect = racing_read
    with pytest.raises(CycleBlockedError, match="recovery_preview_changed"):
        await call(cycle, req, accept=True, remote=remote)
    assert (await state(cycle))[1].state == "held"


async def test_refused_claim_release_rolls_back_execution_conclusion(cycle, monkeypatch):
    req, _ = await prepare(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    before = (await state(cycle))[0]
    monkeypatch.setattr(
        settlement,
        "release_work",
        AsyncMock(
            return_value=ClaimReceipt(
                disposition=Disposition.BLOCKED,
                claim_id=cycle.identity.claim_id,
                generation=cycle.identity.claim_generation,
                reason="continuation_outstanding",
            )
        ),
    )
    with pytest.raises(CycleBlockedError, match="reviewer_claim_changed"):
        await call(cycle, req, accept=True)
    after, claim, node, _ = await state(cycle)
    assert after.revision == before.revision and after.status == before.status
    assert claim.state == "held" and node.state == "running"


async def test_recovery_mode_requires_human_and_routes_to_settlement(cycle, monkeypatch):
    req, _ = await prepare(cycle)
    handler = AsyncMock(return_value={"snapshot": "x"})
    monkeypatch.setattr(settlement, "request_settlement", handler)
    args = dict(org_id=ORG, node_id=cycle.node.id, actor_id="human", actor_role="owner", request=req)
    assert await request_review_recovery(None, **args) == {"snapshot": "x"}
    with pytest.raises(CycleBlockedError, match="human_plan_approver_required"):
        await request_review_recovery(None, **args, autonomous=True)
    assert handler.await_count == 1


async def predecessor(ctx):
    run = settlement.attempt_run_id(ctx.node.id, 3)
    async with ctx.factory() as db:
        old = OrchestrationExecution(
            org_id=ORG,
            flow_id=ctx.flow.id,
            node_id=ctx.node.id,
            cycle=3,
            phase="admitted",
            status="awaiting_external",
            revision=119,
            accepted_plan_version=1,
            claim_id=ctx.identity.claim_id,
            claim_generation=ctx.identity.claim_generation - 2,
            attempts=0,
            next_check_at=datetime.now(UTC),
        )
        db.add(old)
        await db.commit()
    raw = dict(
        pk={"S": f"TENANT#{ORG}"},
        sk={"S": f"EXEC#{run}"},
        invocation_id={"S": run},
        tenant_id={"S": ORG},
        flow_id={"S": ctx.flow.id},
        orchestration_node_id={"S": ctx.node.id},
        orchestration_node_attempt={"N": "3"},
        repo={"S": ctx.binding.repo},
        status={"S": "completed"},
        terminal_outcome={"S": "failed"},
        workload_binding={"S": "old-pod"},
        terminal_report_digest={"S": "d" * 64},
        terminal_recorded_at={"S": datetime.now(UTC).isoformat()},
    )
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    return old, raw


@pytest.mark.parametrize("cycle", [{"node_attempt": 5}], indirect=True)
async def test_failed_successor_also_retires_verified_obsolete_admission_for_amendment(cycle):
    req, current_raw = await prepare(cycle)
    old, raw = await predecessor(cycle)
    preview = await call(cycle, req)
    proof = preview["retired_admission_evidence"]
    assert len(proof) == 1 and proof[0]["execution_id"] == old.id
    assert proof[0]["terminal_outcome"] == "failed" and proof[0]["revision"] == 119
    async with cycle.factory() as db:
        assert (await db.get(OrchestrationExecution, old.id)).status == "awaiting_external"
    req.expected_snapshot = preview["snapshot"]
    result = await call(cycle, req, accept=True)
    assert result["created"] is True
    async with cycle.factory() as db:
        retired = await db.get(OrchestrationExecution, old.id)
        assert (retired.status, retired.phase, retired.revision, retired.next_check_at) == ("superseded", "concluded", 120, None)
        assert (retired.cycle, retired.claim_generation, retired.attempts, retired.accepted_plan_version) == (3, old.claim_generation, 0, 1)
    assert (await state(cycle))[2].state == "failed"
    assert await quiescence(cycle) == []
    assert (await call(cycle, req, accept=True))["created"] is False
    for saved in (raw, current_raw):
        assert cycle.store._read(saved["pk"]["S"], saved["sk"]["S"]) == saved


@pytest.mark.parametrize("cycle", [{"node_attempt": 5}], indirect=True)
@pytest.mark.parametrize("change", ["active", "wrong_node", "missing_exit", "pending", "action", "generation", "revision", "future_cycle"])
async def test_obsolete_admission_needs_exact_exit_and_no_effects_before_any_settlement(cycle, change):
    req, _ = await prepare(cycle)
    old, raw = await predecessor(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    before = (await state(cycle))[0]
    if change in {"active", "wrong_node", "missing_exit"}:
        if change == "active":
            raw["status"] = {"S": "active"}
        elif change == "wrong_node":
            raw["orchestration_node_id"] = {"S": "different-node"}
        else:
            raw.pop("terminal_report_digest")
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    else:
        async with cycle.factory() as db:
            current = await db.get(OrchestrationExecution, old.id)
            if change == "pending":
                current.pending_action_key = "unresolved"
            elif change == "generation":
                current.claim_generation = cycle.identity.claim_generation
            elif change == "revision":
                current.revision += 1
            elif change == "future_cycle":
                current.cycle = 6
            else:
                db.add(
                    OrchestrationAction(
                        org_id=ORG, execution_id=old.id, operation_key="hidden-effect", kind="review_cycle_dispatch", status="succeeded"
                    )
                )
            await db.commit()
    with pytest.raises(CycleBlockedError):
        await call(cycle, req, accept=True)
    after, claim, node, _ = await state(cycle)
    assert after.revision == before.revision and after.status == before.status
    assert claim.state == "held" and node.state == "running"
    async with cycle.factory() as db:
        assert (await db.get(OrchestrationExecution, old.id)).status == "awaiting_external"
        assert not list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == KIND)))


@pytest.mark.parametrize("cycle", [{"node_attempt": 5}], indirect=True)
async def test_predecessor_retirement_refusal_rolls_back_current_failure_settlement(cycle, monkeypatch):
    req, _ = await prepare(cycle)
    old, _ = await predecessor(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    before = (await state(cycle))[0]
    monkeypatch.setattr(settlement, "supersede_replaced_admission", AsyncMock(return_value=False))
    with pytest.raises(CycleBlockedError, match="replaced_admission_changed"):
        await call(cycle, req, accept=True)
    after, claim, node, _ = await state(cycle)
    assert after.revision == before.revision and after.status == before.status
    assert claim.state == "held" and node.state == "running"
    async with cycle.factory() as db:
        assert (await db.get(OrchestrationExecution, old.id)).status == "awaiting_external"
        assert not list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == KIND)))


async def test_same_snapshot_with_changed_reason_cannot_rewrite_audit(cycle):
    req, _ = await prepare(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    req.reason = "Try to change the historical decision explanation"
    with pytest.raises(CycleBlockedError, match="recovery_receipt_conflict"):
        await call(cycle, req, accept=True)


async def test_identical_accept_that_loses_a_race_replays_the_one_audit(cycle):
    req, _ = await prepare(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    remote = provider(cycle)
    value = remote.read.return_value

    async def earlier_accept(binding):
        await call(cycle, req, accept=True)
        return value

    remote.read.side_effect = earlier_accept
    # The other transaction commits while this caller is reading GitHub. Its
    # immutable response-lost receipt is returned before checking changed state.
    result = await call(cycle, req, accept=True, remote=remote)
    assert result["created"] is False
    async with cycle.factory() as db:
        audits = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == KIND)))
        assert len(audits) == 1
