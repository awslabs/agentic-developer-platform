"""Owner terminal settlement using real PostgreSQL, accepted review bytes and GitHub reads."""

# ruff: noqa: F811

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from src.orchestration.execution_state import OutcomeKind
from src.orchestration.execution_store import load_execution
from src.orchestration.merge_controller import MERGE_KIND, MergeReceipt
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.protected_merge_recovery import request_recovery
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.review_recovery import ReviewRecoveryRequest
from src.orchestration.work_claims import ClaimBinding, ClaimOwner, OwnerKind, claim_work
from tests.orchestration.test_merge_controller import merge, prepared_merge  # noqa: F401
from tests.orchestration.test_protected_developer_recovery import amend_to_v3
from tests.orchestration.test_review_cycle import ORG, cycle, pg_server, pg_url, state, store  # noqa: F401
from tests.orchestration.test_settlement_evidence_fingerprint import reordered

pytestmark = pytest.mark.parametrize("cycle", [{"plan_version": 2, "claim_generation": 2}], indirect=True)


async def stranded(ctx, *, phase="merge_ready", status="runnable"):
    execution, claim, _, _ = await state(ctx)
    reviewer = claim.active_run_id
    raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{reviewer}")
    raw.update(
        workload_binding={"S": "pod:completed-reviewer"},
        terminal_report_digest={"S": "e" * 64},
        terminal_recorded_at={"S": datetime.now(UTC).isoformat()},
    )
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    # Simulate the old auxiliary writer. The current release guard must refuse
    # this transition; this fixture does not change or bypass production policy.
    async with ctx.factory() as db:
        claim = await db.get(OrchestrationWorkClaim, claim.id)
        claim.state, claim.active_run_id, claim.release_reason = "released", None, "completed"
        claim.claim_event_id, claim.released_at = reviewer, datetime.now(UTC)
        # The original developer's missing handoff must remain missing even
        # though a later owner recovery obtained an authentic accepted review.
        current = await db.get(OrchestrationExecution, execution.id)
        current.handoff_receipt_ref = None
        current.phase, current.status = phase, status
        await db.commit()
    ctx.merge_remote()
    return ReviewRecoveryRequest(
        mode="settle_merged",
        expected_attempt=1,
        expected_plan_version=ctx.plan.version,
        expected_run_id=reviewer,
        expected_head_sha=ctx.head,
        reason="Owner accepts verified existing delivery after premature claim release.",
    ), raw


async def call(ctx, request, *, accept=False, actor_id="human"):
    async with ctx.factory() as db:
        result = await request_recovery(
            db,
            org_id=ORG,
            node_id=ctx.node.id,
            actor_id=actor_id,
            actor_role="owner",
            request=request,
            accept=accept,
            services=ctx.merge_services,
        )
        if accept:
            await db.commit()
        return result


async def history(ctx):
    async with ctx.factory() as db:
        actions = list(await db.scalars(select(OrchestrationAction).order_by(OrchestrationAction.id)))
        decisions = list(await db.scalars(select(OrchestrationDecision).order_by(OrchestrationDecision.id)))
        return [dict(id=r.id, kind=r.kind, status=r.status, detail=r.detail, artifact=r.artifact_ref, attempt=r.attempt) for r in actions], [
            r.id for r in decisions
        ]


async def legacy_stall(ctx, **overrides):
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.node.id)
        node.state = "failed"
        values = dict(
            org_id=ORG,
            flow_id=node.flow_id,
            node_id=node.id,
            kind="node_stalled",
            actor_id="system:orchestration-stall-detector",
            actor_kind="service",
            actor_role="engine",
            from_state="running",
            to_state="failed",
            reason="stalled: 19477s in 'running' exceeds the 19440s threshold (agent pod deadline 21600s)",
        )
        event = OrchestrationDecision(**(values | overrides))
        db.add(event)
        await db.commit()
        return event.id


async def test_released_completed_merge_wait_has_no_legacy_worker_stall_clock(merge):
    from src.orchestration.stall import _continuation_clock

    await stranded(merge)
    candidate = SimpleNamespace(org_id=ORG, flow_id=merge.node.flow_id, node_id=merge.node.id, attempts=1)
    async with merge.factory() as db:
        assert await _continuation_clock(db, candidate) == (True, None)


async def test_exact_legacy_stall_settles_existing_merge_without_replaying_attempt(merge):
    req, raw = await stranded(merge)
    event_id = await legacy_stall(merge)
    before = await state(merge)
    preview = await call(merge, req)
    assert preview["legacy_stall"]["decision_id"] == event_id
    req.expected_snapshot = preview["snapshot"]
    accepted = await call(merge, req, accept=True)
    assert accepted["created"]
    after = await state(merge)
    assert after[0].phase == after[0].status == "concluded" and after[2].state == "passed"
    assert after[0].id == before[0].id and after[0].revision == before[0].revision + 1
    assert after[0].claim_generation == after[1].generation == 2 and after[2].attempts == before[2].attempts == 1
    assert after[1].state == "released" and after[1].claim_event_id == req.expected_run_id and after[1].active_run_id is None
    assert after[0].attempts == before[0].attempts and after[0].handoff_receipt_ref is None
    assert merge.store._read(f"TENANT#{ORG}", f"EXEC#{req.expected_run_id}") == raw
    assert not merge.mutations
    async with merge.factory() as db:
        event = await db.get(OrchestrationDecision, event_id)
        assert event.from_state == "running" and event.to_state == "failed"
    assert (await call(merge, req, accept=True))["created"] is False


@pytest.mark.parametrize(
    "change",
    [
        "actor",
        "actor_kind",
        "actor_role",
        "kind",
        "from_state",
        "partial_transition",
        "reason",
        "fenced",
        "threshold",
        "old",
        "future",
        "execution_moved",
        "manual_failure",
    ],
)
async def test_failed_projection_requires_current_exact_legacy_stall_provenance(merge, change):
    req, _ = await stranded(merge)
    before = await state(merge)
    overrides = {
        "actor": {"actor_id": "other-service"},
        "actor_kind": {"actor_kind": "human"},
        "actor_role": {"actor_role": "owner"},
        "kind": {"kind": "result_observed"},
        "from_state": {"from_state": "ready"},
        "partial_transition": {"from_state": None},
        "reason": {"reason": "arbitrary failure"},
        "fenced": {"reason": json.dumps({"fence": "unacknowledged_stall"})},
        "threshold": {"reason": "stalled: 10s in 'running' exceeds the 19440s threshold (agent pod deadline 21600s)"},
        "old": {"created_at": before[1].released_at - timedelta(seconds=1)},
        "future": {"created_at": datetime.now(UTC) + timedelta(hours=1)},
    }.get(change, {})
    await legacy_stall(merge, **overrides)
    if change in {"execution_moved", "manual_failure"}:
        async with merge.factory() as db:
            if change == "execution_moved":
                execution = await db.get(OrchestrationExecution, before[0].id)
                execution.revision += 1
            else:
                db.add(
                    OrchestrationDecision(
                        org_id=ORG,
                        flow_id=merge.node.flow_id,
                        node_id=merge.node.id,
                        kind="node_failed",
                        actor_kind="human",
                        actor_id="human",
                        actor_role="owner",
                        from_state="running",
                        to_state="failed",
                        reason="Manual failure after the old stall",
                    )
                )
            await db.commit()
    old_history = await history(merge)
    with pytest.raises(CycleBlockedError, match="merge_settlement_stall_provenance_changed"):
        await call(merge, req)
    assert await history(merge) == old_history and not merge.mutations
    assert (await state(merge))[2].state == "failed"


@pytest.mark.parametrize("change", ["release_failed", "active_run", "generation", "event", "phase", "pending_action"])
async def test_legacy_stall_clock_exemption_requires_exact_completed_merge_assignment(merge, change):
    from src.orchestration.stall import _continuation_clock

    await stranded(merge)
    current = await state(merge)
    async with merge.factory() as db:
        claim = await db.get(OrchestrationWorkClaim, current[1].id)
        execution = await db.get(OrchestrationExecution, current[0].id)
        if change == "release_failed":
            claim.release_reason = "failed"
        elif change == "active_run":
            claim.active_run_id = "new-worker"
        elif change == "generation":
            claim.generation += 1
        elif change == "event":
            claim.claim_event_id = "other-reviewer"
        elif change == "phase":
            execution.phase = "review_pending"
        else:
            execution.pending_action_key = "unsettled-effect"
        await db.flush()
        candidate = SimpleNamespace(org_id=ORG, flow_id=merge.node.flow_id, node_id=merge.node.id, attempts=1)
        assert await _continuation_clock(db, candidate) == (False, None)


async def test_legacy_stall_proposal_rechecks_merge_wait_under_flow_lock(merge, monkeypatch):
    from src.orchestration import stall
    from src.orchestration.models import DecisionKind
    from src.orchestration.state import NodeState

    await stranded(merge)
    apply = AsyncMock(side_effect=AssertionError("Stale clock must not fail this completed merge wait"))
    monkeypatch.setattr(stall, "apply_guarded_transition", apply)
    node = (await state(merge))[2]
    candidate = SimpleNamespace(
        org_id=ORG,
        flow_id=merge.node.flow_id,
        node_id=merge.node.id,
        attempts=1,
        observed_state="running",
        since=node.updated_at or node.created_at,
    )
    async with merge.factory() as db:
        await stall._propose(
            db,
            candidate,
            stall.StallReport(),
            to_state=NodeState.FAILED,
            kind=DecisionKind.NODE_STALLED,
            reason="stale clock",
            detail={},
            detected_counter="stalls_detected",
            notify_config=None,
        )
    apply.assert_not_awaited()
    assert (await state(merge))[2].state == "running"


@pytest.mark.parametrize("phase,status", [("merge_ready", "runnable"), ("awaiting_review", "runnable"), ("awaiting_review", "awaiting_external")])
async def test_preview_accept_and_replay_preserve_generation_review_attempts_spend_and_plan3(merge, monkeypatch, phase, status):
    ctx = merge
    req, raw = await stranded(ctx, phase=phase, status=status)
    await amend_to_v3(ctx, monkeypatch)
    req.expected_plan_version = 3
    # Conclusion is observation, not a new paid admission. Exhausted spend stays
    # exhausted and cannot cause replay of the successful reviewer.
    ctx.spend = Decimal(100)
    before = await state(ctx)
    old_history = await history(ctx)
    calls = list(ctx.calls)
    preview = await call(ctx, req)
    assert preview["accepted_plan_version"] == 2 and preview["plan_version"] == 3 and preview["claim_generation"] == 2
    assert await history(ctx) == old_history
    assert (await state(ctx))[0].revision == before[0].revision
    req.expected_snapshot = preview["snapshot"]
    accepted = await call(ctx, req, accept=True)
    assert accepted["created"]
    execution, claim, node, _ = await state(ctx)
    assert execution.id == before[0].id and execution.revision == before[0].revision + 1
    assert execution.phase == execution.status == "concluded" and execution.next_check_at is None and node.state == "passed"
    assert execution.claim_generation == claim.generation == 2 and execution.accepted_plan_version == 2
    assert claim.state == "released" and claim.claim_event_id == req.expected_run_id and claim.active_run_id is None
    assert execution.attempts == before[0].attempts and node.attempts == 1
    assert execution.handoff_receipt_ref == before[0].handoff_receipt_ref is None and ctx.spend == Decimal(100)
    assert ctx.calls == calls and not ctx.mutations
    assert ctx.store._read(f"TENANT#{ORG}", f"EXEC#{req.expected_run_id}") == raw
    after_history = await history(ctx)
    from src.orchestration.story_acceptance import KIND as ACCEPTANCE_KIND

    assert [r for r in after_history[0] if r["kind"] not in {MERGE_KIND, ACCEPTANCE_KIND}] == old_history[0]
    acceptance = [r for r in after_history[0] if r["kind"] == ACCEPTANCE_KIND]
    assert len(acceptance) == 1 and acceptance[0]["detail"]["completed_at"] is not None
    action = next(r for r in after_history[0] if r["kind"] == MERGE_KIND)
    receipt = MergeReceipt.model_validate(action["detail"]["merge_receipt"])
    assert receipt.claim_generation == 2 and receipt.accepted_plan_version == 2 and receipt.adopted
    assert action["detail"]["merge_mutation_performed"] is False and action["attempt"] == before[0].attempts
    assert len(after_history[1]) == len(old_history[1]) + 3
    async with ctx.factory() as db:
        created = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.id.not_in(old_history[1]))))
        assert {event.kind for event in created} == {"result_observed", "merged_delivery_settled", "execution_phase_changed"}
        phase_event = next(event for event in created if event.kind == "execution_phase_changed")
        assert phase_event.actor_id == "system:execution-store" and phase_event.actor_kind == "service"
        assert json.loads(phase_event.reason) == {
            "execution_id": execution.id,
            "cycle": execution.cycle,
            "previous_revision": before[0].revision,
            "revision": execution.revision,
            "from_phase": phase,
            "from_status": status,
            "to_phase": "concluded",
            "to_status": "concluded",
            "claim_id": claim.id,
            "claim_generation": claim.generation,
        }
    assert (await call(ctx, req, accept=True))["created"] is False
    assert await history(ctx) == after_history
    req.reason = "A changed request cannot replay the original owner settlement."
    with pytest.raises(CycleBlockedError, match="recovery_receipt_conflict"):
        await call(ctx, req, accept=True)
    async with ctx.factory() as db:
        assert (await load_execution(db, identity=ctx.identity)).kind is OutcomeKind.CONFLICT
    for mint in ctx.mint.call_args_list:
        assert all(value == "read" for value in mint.kwargs["permissions"].values())


@pytest.mark.parametrize(
    "phase,status",
    [
        ("awaiting_review", "blocked"),
        ("preparing", "runnable"),
        ("repairing", "awaiting_external"),
        ("merge_ready", "blocked"),
        ("deployment_pending", "runnable"),
    ],
)
async def test_unfinished_or_blocked_phases_cannot_settle_existing_merge(merge, phase, status):
    req, _ = await stranded(merge, phase=phase, status=status)
    before = await history(merge)
    with pytest.raises(CycleBlockedError, match="merge_settlement_phase_changed"):
        await call(merge, req)
    assert await history(merge) == before and not merge.mutations


async def test_concurrent_accept_has_one_terminal_receipt(merge):
    req, _ = await stranded(merge)
    req.expected_snapshot = (await call(merge, req))["snapshot"]
    before = await state(merge)
    results = await asyncio.gather(call(merge, req, accept=True), call(merge, req, accept=True))
    assert sorted(r["created"] for r in results) == [False, True]
    assert (await state(merge))[0].revision == before[0].revision + 1
    assert len([r for r in (await history(merge))[0] if r["kind"] == MERGE_KIND]) == 1


async def test_repository_metadata_changes_preserve_preview_and_current_raw_audit(merge):
    req, _ = await stranded(merge)
    merge.remote["repository"].update(temp_clone_token="synthetic-first", pushed_at="2026-10-01T00:00:00Z", stargazers_count=1)
    before = await call(merge, req)
    req.expected_snapshot = before["snapshot"]
    merge.remote["repository"].update(temp_clone_token="synthetic-second", pushed_at="2026-10-02T00:00:00Z", stargazers_count=2)
    after = await call(merge, req)
    assert after["snapshot"] == before["snapshot"]

    def repository(preview):
        return next(s for s in preview["provider_verification"]["check_sources"] if s["kind"] == "repository_settings")

    assert repository(before)["digest"] != repository(after)["digest"]
    assert repository(before)["semantic_digest"] == repository(after)["semantic_digest"]
    accepted = await call(merge, req, accept=True)
    assert accepted["created"] and repository(accepted) == repository(after)
    action = next(row for row in (await history(merge))[0] if row["kind"] == MERGE_KIND)
    verification = action["detail"]["post_merge_verification"]
    assert next(s for s in verification["check_sources"] if s["kind"] == "repository_settings") == repository(after)
    assert (
        action["detail"]["merge_receipt"]["eligibility_digest"]
        == hashlib.sha256(json.dumps(verification, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    async with merge.factory() as db:
        decision = await db.get(OrchestrationDecision, accepted["decision_id"])
        assert repository(json.loads(decision.reason)) == repository(after)
    assert not merge.mutations


async def test_embedded_repository_token_rotation_preserves_settlement_and_fresh_audit(merge):
    req, _ = await stranded(merge)
    for side in ("head", "base"):
        merge.remote["pr"][side]["repo"].update(
            temp_clone_token="synthetic-first",
            pushed_at="2026-10-01T00:00:00Z",
            updated_at="2026-10-01T00:00:00Z",
            size=100,
            open_issues_count=5,
            stargazers_count=10,
            forks_count=1,
            watchers_count=10,
        )
    before = await call(merge, req)
    req.expected_snapshot = before["snapshot"]
    for side in ("head", "base"):
        merge.remote["pr"][side]["repo"].update(
            temp_clone_token="synthetic-second",
            pushed_at="2026-10-02T00:00:00Z",
            updated_at="2026-10-02T00:00:00Z",
            size=105,
            open_issues_count=4,
            stargazers_count=11,
            forks_count=2,
            watchers_count=11,
        )
    after = await call(merge, req)
    assert after["snapshot"] == before["snapshot"]
    kinds = {"pull_request", "pull_request_recheck"}
    old = {s["kind"]: s for s in before["provider_verification"]["check_sources"] if s["kind"] in kinds}
    fresh = {s["kind"]: s for s in after["provider_verification"]["check_sources"] if s["kind"] in kinds}
    for kind in kinds:
        assert old[kind]["digest"] != fresh[kind]["digest"]
        assert old[kind]["semantic_digest"] == fresh[kind]["semantic_digest"]
    accepted = await call(merge, req, accept=True)
    assert accepted["created"]
    action = next(row for row in (await history(merge))[0] if row["kind"] == MERGE_KIND)
    verification = action["detail"]["post_merge_verification"]
    assert {s["kind"]: s for s in verification["check_sources"] if s["kind"] in kinds} == fresh
    assert (
        action["detail"]["merge_receipt"]["eligibility_digest"]
        == hashlib.sha256(json.dumps(verification, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    assert not merge.mutations


async def test_complete_provider_json_reserialization_preserves_acceptance_and_fresh_audit(merge, monkeypatch):
    req, _ = await stranded(merge)
    before = await call(merge, req)
    req.expected_snapshot = before["snapshot"]
    client = merge.merge_services.provider.client
    original = client._transport

    async def reserialized(request):
        response = await original.handle_async_request(request)
        payload = json.dumps(reordered(response.json()), indent=1).encode() + b"\n"
        return httpx.Response(response.status_code, content=payload)

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(reserialized))
    after = await call(merge, req)
    assert after["snapshot"] == before["snapshot"]
    assert after["provider_verification"]["check_sources"] != before["provider_verification"]["check_sources"]
    assert (await call(merge, req, accept=True))["created"]
    action = next(row for row in (await history(merge))[0] if row["kind"] == MERGE_KIND)
    assert action["detail"]["post_merge_verification"]["check_sources"] == after["provider_verification"]["check_sources"]
    assert not merge.mutations


@pytest.mark.parametrize(
    "path,value",
    [
        (("head", "repo", "private"), True),
        (("base", "repo", "archived"), True),
        (("head", "repo", "default_branch"), "release"),
        (("base", "repo", "permissions"), {"push": False}),
        (("head", "repo", "allow_merge_commit"), False),
        (("base", "repo", "allow_squash_merge"), False),
        (("head", "repo", "allow_rebase_merge"), False),
        (("base", "repo", "future_security_rule"), {"enabled": True}),
        (("head", "repo", "id"), 999),
        (("head", "ref"), "changed-source-branch"),
        (("head", "sha"), "d" * 40),
        (("base", "sha"), "d" * 40),
        (("base", "ref"), "release"),
        (("merge_commit_sha",), "d" * 40),
        (("merged_at",), "2026-10-02T00:00:00Z"),
        (("updated_at",), "2026-10-02T00:00:00Z"),
    ],
)
async def test_pr_authority_drift_refuses_old_settlement_without_writes(merge, path, value):
    req, _ = await stranded(merge)
    before = await call(merge, req)
    req.expected_snapshot = before["snapshot"]
    old_history = await history(merge)
    old_state = await state(merge)
    document = merge.remote["pr"]
    for key in path[:-1]:
        document = document[key]
    document[path[-1]] = value
    with pytest.raises(CycleBlockedError):
        await call(merge, req, accept=True)
    assert await history(merge) == old_history and not merge.mutations
    current = await state(merge)
    assert current[0].revision == old_state[0].revision and current[0].phase == "merge_ready"
    assert current[1].generation == old_state[1].generation == 2


@pytest.mark.parametrize("change", ["merge_method", "merge_setting_presence", "required_approvals"])
async def test_repository_semantics_still_invalidate_settlement_preview(merge, change):
    req, _ = await stranded(merge)
    before = await call(merge, req)
    req.expected_snapshot = before["snapshot"]
    if change == "merge_method":
        merge.remote["repository"]["allow_merge_commit"] = False
        merge.remote["graphql"]["data"]["repository"]["mergeCommitAllowed"] = False
    elif change == "merge_setting_presence":
        merge.remote["repository"].pop("allow_merge_commit")
    else:
        merge.remote["rules"][1]["parameters"]["required_approving_review_count"] = 0
    after = await call(merge, req)
    assert after["snapshot"] != before["snapshot"]
    old_history = await history(merge)
    with pytest.raises(CycleBlockedError, match="recovery_preview_changed"):
        await call(merge, req, accept=True)
    assert await history(merge) == old_history and not merge.mutations
    assert (await state(merge))[0].phase == "merge_ready"


@pytest.mark.parametrize(
    "change",
    [
        "active",
        "failed",
        "revoked",
        "workload",
        "terminal_digest",
        "terminal_time",
        "grant",
        "head",
        "unmerged",
        "review",
        "checks_pending",
        "checks_failed",
        "missing_check",
        "binding",
        "scope",
        "revision",
        "phase",
        "execution_status",
        "release_time",
        "release_event",
        "successor",
        "policy",
        "expired",
        "paused",
        "actor",
        "review_evidence",
        "artifact",
        "merge_action",
        "merge_sha",
        "provider_unavailable",
    ],
)
@pytest.mark.parametrize("phase,status", [("merge_ready", "runnable"), ("awaiting_review", "awaiting_external")])
async def test_accept_refuses_changed_or_unverified_evidence(merge, change, monkeypatch, phase, status):
    ctx = merge
    req, raw = await stranded(ctx, phase=phase, status=status)
    req.expected_snapshot = (await call(ctx, req))["snapshot"]
    actor = "human"
    if change in {"active", "failed", "revoked", "workload", "terminal_digest", "terminal_time"}:
        if change in {"workload", "terminal_digest", "terminal_time"}:
            raw.pop({"workload": "workload_binding", "terminal_digest": "terminal_report_digest", "terminal_time": "terminal_recorded_at"}[change])
        else:
            raw["terminal_outcome" if change == "failed" else "status"] = {"S": change}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    elif change == "grant":
        ctx.store.authority.revoke_grant(principal=req.expected_run_id + "#1", tenant_id=ORG)
    elif change == "actor":
        actor = "other"
        monkeypatch.setattr("src.orchestration.protected_merge_recovery.policy_owner_matches", AsyncMock(return_value=False))
    elif change == "artifact":
        ctx.objects[ctx.artifact_key] = b"{}"
    elif change == "provider_unavailable":
        ctx.unavailable = True
    elif change == "merge_sha":
        ctx.remote["pr"]["merge_commit_sha"] = "f" * 40
        ctx.remote["graphql"]["data"]["repository"]["pullRequest"]["mergeCommit"] = {"oid": "f" * 40}
    elif change in {"head", "unmerged", "review"}:
        pr = ctx.remote["graphql"]["data"]["repository"]["pullRequest"]
        if change == "head":
            pr["headRefOid"] = "f" * 40
        elif change == "unmerged":
            ctx.remote["pr"]["merged"], pr["merged"] = False, False
        else:
            pr["reviewDecision"] = "CHANGES_REQUESTED"
            pr["reviews"]["nodes"][0]["state"] = "CHANGES_REQUESTED"
    elif change in {"checks_pending", "checks_failed", "missing_check"}:
        if change == "missing_check":
            ctx.remote["checks"]["check_runs"] = []
            ctx.remote["checks"]["total_count"] = 0
        else:
            ctx.remote["checks"]["check_runs"][0].update(
                status="in_progress" if change == "checks_pending" else "completed",
                conclusion=None if change == "checks_pending" else "failure",
            )
    else:
        async with ctx.factory() as db:
            if change == "successor":
                await claim_work(db, binding=ClaimBinding(ORG, 123, 43), owner=ClaimOwner(OwnerKind.ENGINE_FLOW, ctx.flow.id), event_id="successor")
            elif change == "revision":
                execution = await db.get(OrchestrationExecution, ctx.execution.id)
                execution.revision += 1
            elif change in {"phase", "execution_status"}:
                execution = await db.get(OrchestrationExecution, ctx.execution.id)
                if change == "phase":
                    execution.phase = "awaiting_review" if phase == "merge_ready" else "merge_ready"
                    execution.status = "runnable"
                else:
                    execution.status = "blocked"
            elif change == "paused":
                (await db.get(OrchestrationFlow, ctx.flow.id)).execution_paused = True
            elif change in {"release_time", "release_event"}:
                claim = await db.get(OrchestrationWorkClaim, ctx.identity.claim_id)
                if change == "release_time":
                    claim.released_at += timedelta(seconds=1)
                else:
                    claim.claim_event_id = "other"
            elif change in {"policy", "expired"}:
                plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
                policy = dict(plan.plan_document["execution_policy"])
                policy["policy_hash" if change == "policy" else "expires_at"] = (
                    "c" * 64 if change == "policy" else (datetime.now(UTC) - timedelta(hours=1)).isoformat()
                )
                plan.plan_document = {"execution_policy": policy}
            elif change == "review_evidence":
                action = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "review_evidence"))
                action.detail = dict(action.detail, publication_outstanding="true")
            elif change == "merge_action":
                db.add(
                    OrchestrationAction(org_id=ORG, execution_id=ctx.execution.id, operation_key="uncertain-merge", kind=MERGE_KIND, status="unknown")
                )
            else:
                binding = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
                if change == "binding":
                    binding.revision += 1
                else:
                    binding.accepted_scope = "{}"
            await db.commit()
    unchanged = await history(ctx)
    with pytest.raises(CycleBlockedError):
        await call(ctx, req, accept=True, actor_id=actor)
    assert await history(ctx) == unchanged and not ctx.mutations
    assert (await state(ctx))[2].state == "running"


async def test_deployment_delivery_is_not_terminal_settlement(cycle, monkeypatch):
    async with prepared_merge(cycle, monkeypatch) as ctx:
        req, _ = await stranded(ctx)
        async with ctx.factory() as db:
            plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
            document = json.loads(json.dumps(plan.plan_document))
            document["execution_policy"]["allowed_actions"].append("deploy")
            plan.plan_document = document
            await db.commit()
        with pytest.raises(CycleBlockedError, match="code_only_settlement_required"):
            await call(ctx, req)


@pytest.mark.parametrize("change", ["repository", "org", "merge_gate"])
async def test_current_policy_must_still_authorize_this_delivery(merge, monkeypatch, change):
    req, _ = await stranded(merge)
    await amend_to_v3(merge, monkeypatch)
    req.expected_plan_version = 3
    async with merge.factory() as db:
        plan = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.version == 3))
        document = json.loads(json.dumps(plan.plan_document))
        field, value = {"repository": ("repository_ids", ["other/repo"]), "org": ("org_id", "other-org"), "merge_gate": ("human_gates", ["merge"])}[
            change
        ]
        document["execution_policy"][field] = value
        plan.plan_document = document
        await db.commit()
    with pytest.raises(CycleBlockedError):
        await call(merge, req)


@pytest.mark.parametrize("change", ["definition", "prerequisite", "actor"])
async def test_unpreserved_plan3_refuses_prior_review(merge, monkeypatch, change):
    req, _ = await stranded(merge)
    await amend_to_v3(merge, monkeypatch, change)
    req.expected_plan_version = 3
    with pytest.raises(CycleBlockedError, match="current_execution_changed"):
        await call(merge, req)


async def test_standing_negative_app_review_cannot_be_withdrawn_by_recovery(merge, monkeypatch):
    from dataclasses import replace

    from src.orchestration.merge_evidence import GitHubEvidenceSource

    req, _ = await stranded(merge)
    req.expected_snapshot = (await call(merge, req))["snapshot"]
    original = GitHubEvidenceSource.bound_pull_request

    async def negative(self, **kwargs):
        evidence = await original(self, **kwargs)
        return replace(evidence, review_approved=False, review_state="changes_requested", foreign_changes_requested=False)

    monkeypatch.setattr(GitHubEvidenceSource, "bound_pull_request", negative)
    with pytest.raises(CycleBlockedError, match="merged_pr_review_not_approved"):
        await call(merge, req, accept=True)
    assert not merge.mutations and (await state(merge))[2].state == "running"


async def test_existing_owner_surface_selects_explicit_terminal_mode(merge, monkeypatch):
    from src.orchestration.review_recovery import request_review_recovery

    req, _ = await stranded(merge)
    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.get_engine_authority_writer", lambda: merge.service.writer)
    monkeypatch.setattr("src.orchestration.merge_controller.MergeProvider", lambda: merge.merge_services.provider)
    monkeypatch.setattr("src.orchestration.merge_review.boto3.client", lambda *args, **kwargs: merge.merge_services.storage)
    async with merge.factory() as db:
        preview = await request_review_recovery(db, org_id=ORG, node_id=merge.node.id, actor_id="human", actor_role="owner", request=req)
        assert preview["next_action"] == "conclude_verified_merge"
    async with merge.factory() as db:
        with pytest.raises(CycleBlockedError, match="human_plan_approver_required"):
            await request_review_recovery(
                db,
                org_id=ORG,
                node_id=merge.node.id,
                actor_id="system:stalled-story-review",
                actor_role="engine",
                request=req,
                autonomous=True,
            )


@pytest.mark.parametrize("change", ["successor", "revision"])
async def test_final_database_fence_rejects_race_after_last_provider_read(merge, monkeypatch, change):
    from src.orchestration import protected_merge_recovery as recovery

    req, _ = await stranded(merge)
    req.expected_snapshot = (await call(merge, req))["snapshot"]
    before = await state(merge)
    original = recovery.terminal
    calls = 0

    async def raced(*args, **kwargs):
        nonlocal calls
        answer = await original(*args, **kwargs)
        calls += 1
        if calls == 2:
            async with merge.factory() as db:
                if change == "successor":
                    await claim_work(
                        db,
                        binding=ClaimBinding(ORG, 123, 43),
                        owner=ClaimOwner(OwnerKind.ENGINE_FLOW, merge.flow.id),
                        event_id="new-authorized-owner",
                    )
                else:
                    row = await db.get(OrchestrationExecution, before[0].id)
                    row.revision += 1
                await db.commit()
        return answer

    monkeypatch.setattr(recovery, "terminal", raced)
    old_history = await history(merge)
    with pytest.raises(CycleBlockedError):
        await call(merge, req, accept=True)
    after = await state(merge)
    assert calls == 2
    assert after[0].phase == "merge_ready" and after[2].state == "running"
    assert after[0].attempts == before[0].attempts and after[2].attempts == before[2].attempts
    assert after[1].generation == (3 if change == "successor" else 2)
    assert await history(merge) == old_history and not merge.mutations
