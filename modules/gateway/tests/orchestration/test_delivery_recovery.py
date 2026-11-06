"""Retain and recover real PostgreSQL delivery without replaying merged work."""

# ruff: noqa: F811

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.engine import validate_engine_authority
from src.orchestration.delivery_recovery import request_recovery
from src.orchestration.execution_runner import _advance
from src.orchestration.execution_state import ExecutionPhase, ExecutionStatus, OutcomeKind
from src.orchestration.execution_store import load_execution
from src.orchestration.models import OrchestrationAction, OrchestrationExecution, OrchestrationFlow, OrchestrationWorkClaim
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.review_recovery import ReviewRecoveryRequest
from src.orchestration.work_claims import Disposition, ReleaseReason, release_work
from tests.orchestration.test_deployment_workflows import deployment, tick  # noqa: F401
from tests.orchestration.test_merge_controller import merge  # noqa: F401
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, state, store  # noqa: F401

pytestmark = pytest.mark.parametrize("cycle", [{"delivery": True, "plan_version": 2, "claim_generation": 2}], indirect=True)


async def stranded(ctx):
    execution, claim, _, _ = await state(ctx)
    run_id = claim.active_run_id
    raw = ctx.store._read(f"TENANT#{ctx.node.org_id}", f"EXEC#{run_id}")
    raw.update(
        workload_binding={"S": "pod:completed-reviewer"},
        terminal_report_digest={"S": "e" * 64},
        terminal_recorded_at={"S": datetime.now(UTC).isoformat()},
    )
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    async with ctx.factory() as db:
        row = await db.get(OrchestrationWorkClaim, claim.id)
        row.state, row.active_run_id, row.release_reason = "released", None, "completed"
        row.claim_event_id, row.released_at = run_id, datetime.now(UTC)
        await db.commit()
    return ReviewRecoveryRequest(
        mode="resume_delivery",
        expected_attempt=1,
        expected_plan_version=ctx.plan.version,
        expected_run_id=run_id,
        expected_head_sha=ctx.head,
        reason="Owner authorizes only the pending deployment after verified successful reviewer exit.",
    ), raw


async def call(ctx, request, *, accept=False, actor_id="human"):
    async with ctx.factory() as db:
        result = await request_recovery(
            db,
            org_id=ctx.node.org_id,
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


async def actions(ctx):
    async with ctx.factory() as db:
        rows = list(await db.scalars(select(OrchestrationAction).order_by(OrchestrationAction.id)))
        return [{k: getattr(row, k) for k in ("id", "operation_key", "kind", "status", "detail", "receipt_ref", "attempt")} for row in rows]


async def test_completed_reviewer_cannot_release_pending_authenticated_deployment(deployment):
    ctx = deployment
    execution, claim, _, _ = await state(ctx)
    async with ctx.factory() as db:
        outcome = await release_work(
            db,
            org_id=claim.org_id,
            claim_id=claim.id,
            generation=claim.generation,
            reason=ReleaseReason.COMPLETED,
            terminal_evidence="protected reviewer completion",
        )
        assert outcome.disposition is Disposition.BLOCKED and outcome.reason == "continuation_outstanding"
        await db.commit()
    assert (await state(ctx))[1].state == "held"
    assert (await tick(ctx)).effects_succeeded == 1
    assert len(ctx.dispatches) == 1
    assert (await state(ctx))[0].id == execution.id


async def test_owner_resumes_engine_delivery_preserving_original_receipts_and_fencing_worker(deployment):
    ctx = deployment
    request, raw = await stranded(ctx)
    before = await state(ctx)
    original = await actions(ctx)
    preview = await call(ctx, request)
    assert preview["next_claim_generation"] == 3
    assert preview["attempts_preserved"] and preview["receipts_preserved"]
    assert await actions(ctx) == original and not ctx.dispatches
    request.expected_snapshot = preview["snapshot"]
    accepted = await call(ctx, request, accept=True)
    assert accepted["created"]
    assert (await call(ctx, request, accept=True))["created"] is False
    after = await state(ctx)
    assert after[0].id == before[0].id and after[0].attempts == before[0].attempts
    assert after[0].claim_generation == after[1].generation == 3 and after[1].state == "held"
    assert after[2].attempts == before[2].attempts and after[2].state == "passed"
    assert await actions(ctx) == original
    assert ctx.store._read(f"TENANT#{ctx.node.org_id}", f"EXEC#{request.expected_run_id}") == raw
    async with ctx.factory() as db:
        assert (await load_execution(db, identity=ctx.identity)).kind is OutcomeKind.CONFLICT
        grant = ctx.store.live_grant(
            invocation_id=request.expected_run_id, tenant_id=ctx.node.org_id, attempt=int(raw["current_attempt"]["N"]), now=datetime.now(UTC)
        )
        with pytest.raises(BootstrapRefusedError):
            await validate_engine_authority(session=db, execution=raw, grant=grant, store=ctx.store)
        with pytest.raises(BootstrapRefusedError):
            await validate_engine_authority(
                session=db, execution=raw, grant=grant, store=ctx.store, delivery_identity=replace(ctx.identity, claim_generation=3)
            )
    ctx.identity = replace(ctx.identity, claim_generation=3)
    report = await tick(ctx)
    assert report.effects_succeeded == 1, ((await state(ctx))[0].block_detail, report)
    assert len(ctx.dispatches) == 1
    await tick(ctx)
    assert len(ctx.dispatches) == 1


@pytest.mark.parametrize(
    "field,value",
    [("operation_key", "unrecognized-effect"), ("status", "unknown"), ("receipt_ref", "provider-effect/1")],
)
async def test_recovery_only_exempts_the_completed_display_observation(deployment, field, value):
    from src.orchestration.story_acceptance import KEY

    ctx = deployment
    request, _ = await stranded(ctx)
    async with ctx.factory() as db:
        row = await db.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.execution_id == ctx.execution.id,
                OrchestrationAction.operation_key == KEY,
            )
        )
        assert row is not None
        setattr(row, field, value)
        await db.commit()
    before = await state(ctx)
    with pytest.raises(CycleBlockedError, match="delivery_effect_requires_reconciliation"):
        await call(ctx, request)
    after = await state(ctx)
    assert after[0].revision == before[0].revision and after[1].generation == before[1].generation
    assert not ctx.dispatches


async def test_controller_releases_its_recovered_claim_at_terminal_settlement(deployment):
    ctx = deployment
    request, _ = await stranded(ctx)
    request.expected_snapshot = (await call(ctx, request))["snapshot"]
    await call(ctx, request, accept=True)
    identity = replace(ctx.identity, claim_generation=3)
    async with ctx.factory() as db:
        record = (await load_execution(db, identity=identity)).record
    outcome = await _advance(
        ctx.factory,
        record=record,
        phase=ExecutionPhase.CONCLUDED,
        status=ExecutionStatus.CONCLUDED,
        next_check_at=None,
        progress_note="Observed terminal delivery",
    )
    assert outcome.kind is OutcomeKind.APPLIED
    _, claim, _, _ = await state(ctx)
    assert claim.state == "released" and claim.generation == 3 and claim.active_run_id is None


async def test_recovery_remains_refused_after_new_owner_claims_issue(deployment):
    from src.orchestration.work_claims import ClaimBinding, ClaimOwner, OwnerKind, claim_work

    ctx = deployment
    request, _ = await stranded(ctx)
    request.expected_snapshot = (await call(ctx, request))["snapshot"]
    _, claim, _, _ = await state(ctx)
    async with ctx.factory() as db:
        replacement = await claim_work(
            db,
            binding=ClaimBinding(claim.org_id, claim.provider_repository_id, claim.issue_number),
            owner=ClaimOwner(OwnerKind.DIRECT_DISPATCH, "other-owner"),
            event_id="other-event",
        )
        await db.commit()
    with pytest.raises(CycleBlockedError):
        await call(ctx, request, accept=True)
    assert (await state(ctx))[1].generation == replacement.generation
    assert not ctx.dispatches


async def test_concurrent_acceptance_acquires_only_one_new_generation(deployment):
    ctx = deployment
    request, _ = await stranded(ctx)
    request.expected_snapshot = (await call(ctx, request))["snapshot"]
    results = await asyncio.gather(call(ctx, request, accept=True), call(ctx, request, accept=True), return_exceptions=True)
    assert len([result for result in results if isinstance(result, dict) and result["created"]]) == 1
    assert all(isinstance(result, dict | CycleBlockedError) for result in results)
    assert (await state(ctx))[1].generation == 3
    assert (await call(ctx, request, accept=True))["created"] is False
    assert not ctx.dispatches


@pytest.mark.parametrize("change", ["definition", "prerequisite", "actor"])
async def test_recovery_rejects_unpreserved_or_unapproved_plan(deployment, monkeypatch, change):
    from tests.orchestration.test_protected_developer_recovery import amend_to_v3

    ctx = deployment
    request, _ = await stranded(ctx)
    await amend_to_v3(ctx, monkeypatch, change=change)
    request.expected_plan_version = 3
    with pytest.raises(CycleBlockedError):
        await call(ctx, request)
    assert (await state(ctx))[1].state == "released" and not ctx.dispatches


async def test_preserving_amendment_keeps_delivery_recovery_authorized(deployment, monkeypatch):
    from tests.orchestration.test_deployment_controller import finish, prepare_runtime
    from tests.orchestration.test_protected_developer_recovery import amend_to_v3

    ctx = deployment
    request, _ = await stranded(ctx)
    await amend_to_v3(ctx, monkeypatch)
    request.expected_plan_version = 3
    request.expected_snapshot = (await call(ctx, request))["snapshot"]
    await call(ctx, request, accept=True)
    ctx.identity = replace(ctx.identity, claim_generation=3)
    ctx.entry = replace(ctx.entry, verification_adapter="gateway-health-verification")
    report = await tick(ctx)
    assert report.effects_succeeded == 1, ((await state(ctx))[0].block_detail, report)
    await prepare_runtime(ctx)
    await finish(ctx)
    completed = [
        row["detail"]["deployment_receipt"]
        for row in await actions(ctx)
        if row["kind"] == "deployment_verification" and row["detail"]["deployment_receipt"].get("delivery_complete")
    ]
    assert len(completed) == 1
    assert completed[0]["accepted_plan_version"] == 2 and completed[0]["claim_generation"] == 3


@pytest.mark.parametrize("change", ["actor", "head", "paused", "claim_generation", "failed_terminal", "pending_action", "receipt_source"])
async def test_recovery_refuses_changed_authority_or_evidence(deployment, change):
    ctx = deployment
    request, raw = await stranded(ctx)
    preview = await call(ctx, request)
    request.expected_snapshot = preview["snapshot"]
    if change == "head":
        request.expected_head_sha = "f" * 40
    elif change == "failed_terminal":
        raw["terminal_outcome"] = {"S": "failed"}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    else:
        async with ctx.factory() as db:
            execution, claim, _, _ = await state(ctx)
            if change == "paused":
                (await db.get(OrchestrationFlow, ctx.node.flow_id)).execution_paused = True
            elif change == "claim_generation":
                (await db.get(OrchestrationWorkClaim, claim.id)).generation += 1
            elif change == "pending_action":
                (await db.get(OrchestrationExecution, execution.id)).pending_action_key = "unreconciled-effect"
            elif change == "receipt_source":
                action = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "merge_pull_request"))
                detail = json.loads(json.dumps(action.detail))
                detail["merge_receipt"]["merge_sha"] = "f" * 40
                action.detail = detail
            await db.commit()
    with pytest.raises(CycleBlockedError):
        await call(ctx, request, accept=True, actor_id="different-human" if change == "actor" else "human")
    assert not ctx.dispatches


async def test_terminal_delivery_releases_normally(deployment):
    ctx = deployment
    execution, claim, _, _ = await state(ctx)
    async with ctx.factory() as db:
        row = await db.get(OrchestrationExecution, execution.id)
        row.phase = row.status = "concluded"
        await db.flush()
        outcome = await release_work(
            db,
            org_id=claim.org_id,
            claim_id=claim.id,
            generation=claim.generation,
            reason=ReleaseReason.COMPLETED,
            terminal_evidence="delivery conclusively settled",
        )
        assert outcome.disposition is Disposition.ADMITTED


@pytest.mark.parametrize("pending_status", ["prepared", "dispatched", "unknown"])
async def test_terminal_recovery_cannot_abandon_unresolved_provider_effect(deployment, pending_status):
    from uuid import uuid4

    ctx = deployment
    request, _ = await stranded(ctx)
    request.expected_snapshot = (await call(ctx, request))["snapshot"]
    await call(ctx, request, accept=True)
    identity = replace(ctx.identity, claim_generation=3)
    async with ctx.factory() as db:
        record = (await load_execution(db, identity=identity)).record
        db.add(
            OrchestrationAction(
                id=str(uuid4()),
                org_id=identity.org_id,
                execution_id=record.id,
                operation_key="unresolved-deployment",
                kind="deployment_dispatch",
                status=pending_status,
                attempt=1,
                detail={},
            )
        )
        await db.commit()
    with pytest.raises(CycleBlockedError, match="pending_effect_requires_reconciliation"):
        await _advance(ctx.factory, record=record, phase=ExecutionPhase.CONCLUDED, status=ExecutionStatus.CONCLUDED, next_check_at=None)
    execution, claim, _, _ = await state(ctx)
    assert claim.state == "held" and execution.phase == "deployment_pending"


@pytest.mark.parametrize("pending", [False, True])
async def test_provider_failure_is_a_typed_refusal_without_claim_mutation(deployment, pending):
    from unittest.mock import AsyncMock

    from src.agentauth.github_provider import ProviderUnavailableError
    from src.orchestration.merge_controller import MergeVerificationPendingError

    ctx = deployment
    request, _ = await stranded(ctx)
    ctx.merge_services.provider.read = AsyncMock(
        side_effect=(MergeVerificationPendingError("pending") if pending else ProviderUnavailableError("unavailable"))
    )
    with pytest.raises(CycleBlockedError, match="merged_pr_checks_pending" if pending else "merge_evidence_unavailable"):
        await call(ctx, request)
    assert (await state(ctx))[1].state == "released" and not ctx.dispatches


@pytest.mark.parametrize("tamper", [None, "decision", "receipt"])
async def test_downstream_merge_evaluation_keeps_recovered_history_after_claim_release(deployment, tamper):
    from types import SimpleNamespace

    from src.orchestration.dispatch import graph_address
    from src.orchestration.models import OrchestrationDecision, OrchestrationEdge, OrchestrationNode
    from src.orchestration.repository_evaluation import sources_for
    from src.orchestration.repository_evaluation_contract import RepositoryEvaluationSpecification

    ctx = deployment
    request, _ = await stranded(ctx)
    request.expected_snapshot = (await call(ctx, request))["snapshot"]
    accepted = await call(ctx, request, accept=True)
    identity = replace(ctx.identity, claim_generation=3)
    async with ctx.factory() as db:
        record = (await load_execution(db, identity=identity)).record
    await _advance(ctx.factory, record=record, phase=ExecutionPhase.CONCLUDED, status=ExecutionStatus.CONCLUDED, next_check_at=None)
    async with ctx.factory() as db:
        connection = await db.connection()
        await connection.run_sync(lambda sync: OrchestrationEdge.__table__.create(sync, checkfirst=True))
        parent = await db.get(OrchestrationNode, ctx.node.id)
        flow = await db.get(OrchestrationFlow, parent.flow_id)
        spec = RepositoryEvaluationSpecification.model_validate(
            dict(
                evidence_schema="repository-evaluation/v1",
                runner=dict(adapter="engine-repository-evidence-v1", repository=ctx.binding.repo, repository_id=123, harness_sha256="a" * 64),
                predecessors=[dict(address=graph_address(parent, flow_slug=flow.slug), required_checks=[dict(name="Unit tests", app_id=15368)])],
            )
        )
        node = OrchestrationNode(
            org_id=parent.org_id,
            flow_id=parent.flow_id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="REPOEVAL",
            kind="eval",
            title="Verify merged delivery",
            issue_ref="999",
            state="ready",
            attempts=0,
        )
        db.add(node)
        await db.flush()
        db.add(OrchestrationEdge(org_id=parent.org_id, flow_id=parent.flow_id, from_node_id=parent.id, to_node_id=node.id))
        if tamper == "decision":
            # A missing recovery decision must make the original generation unusable.
            await db.delete(await db.get(OrchestrationDecision, accepted["decision_id"]))
        elif tamper == "receipt":
            action = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "merge_pull_request"))
            detail = json.loads(json.dumps(action.detail))
            detail["merge_receipt"]["merge_sha"] = "f" * 40
            action.detail = detail
        await db.flush()
        if tamper:
            with pytest.raises(CycleBlockedError):
                await sources_for(db, node, SimpleNamespace(version=ctx.plan.version), spec)
        else:
            sources = await sources_for(db, node, SimpleNamespace(version=ctx.plan.version), spec)
            assert len(sources) == 1 and sources[0]["execution_id"] == record.id
            assert sources[0]["head_sha"] == request.expected_head_sha
    assert (await state(ctx))[1].state == "released"
