"""E3 uses real PostgreSQL, E2 failed evidence and the protected source grant."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from src.orchestration.evaluation_correction_state import CORRECTION_KIND, child_id, correction_link
from src.orchestration.evaluation_corrections import EvaluationCorrections
from src.orchestration.evaluation_issue_provider import CorrectionIssue
from src.orchestration.execution_policy import Action
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationAction, OrchestrationNode
from tests.orchestration.test_evaluation_controller import cycle, deployment, evaluation, merge, pg_server, pg_url, runtime, store, tick  # noqa: F401

pytestmark = pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)


@pytest.fixture
async def correction(evaluation):  # noqa: F811
    ctx = evaluation
    ctx.failed_criterion = True
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["allowed_actions"].append(Action.REPAIR.value)
        plan.plan_document = document
        await db.commit()
    await tick(ctx)
    await tick(ctx)
    ctx.issue = None
    ctx.issue_number = 71
    ctx.lost_issue_response = False

    async def find(*args, **kwargs):
        return ctx.issue

    async def create(binding, content, *, reauthorize):
        await reauthorize()
        number = ctx.issue_number
        ctx.issue = CorrectionIssue(
            number,
            f"I_correction_{number}",
            binding.provider_repository_id,
            "open",
            f"https://github.com/{binding.repo}/issues/{number}",
            content["correlation"],
        )
        if ctx.lost_issue_response:
            raise httpx.ReadTimeout("lost after creation")
        return ctx.issue

    ctx.issue_provider = SimpleNamespace(find=AsyncMock(side_effect=find), create=AsyncMock(side_effect=create))
    ctx.corrections = EvaluationCorrections(ctx.evaluation_services, provider=ctx.issue_provider)
    ctx.evaluation_services.corrections = ctx.corrections
    return ctx


@pytest.mark.parametrize("lose_response", [False, True])
async def test_one_issue_and_child_survive_lost_creation_response(correction, lose_response):
    ctx = correction
    ctx.lost_issue_response = lose_response
    first = await tick(ctx)
    assert first.errors == 0
    ctx.issue_provider.create.assert_awaited_once()

    second = await tick(ctx)
    assert second.errors == 0
    async with ctx.factory() as db:
        actions = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == CORRECTION_KIND))).all())
        assert len(actions) == 1
        action = actions[0]
        assert action.detail["creation_started"] and action.status == "succeeded"
        node = await db.get(OrchestrationNode, child_id(action.operation_key))
        assert node is not None and node.kind == "story" and node.state == "ready" and node.issue_ref == "71"
        assert (await correction_link(db, node)).id == action.id
        assert (await db.get(OrchestrationNode, ctx.node.id)).state == "passed"
        assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "running"
    await tick(ctx)
    ctx.issue_provider.create.assert_awaited_once()


async def dispatch_correction(ctx, monkeypatch):
    from src.orchestration import policy_admission
    from src.orchestration.dispatch_pass import prepare_pending, run_dispatch_pass

    for name, value in [("resolve_root_user_entity_id", "human"), ("resolve_user_entity_id", "sub")]:
        monkeypatch.setattr("src.shared.identity.resolver." + name, AsyncMock(return_value=value))
    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", AsyncMock(return_value=42))
    monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=123))
    monkeypatch.setattr("src.orchestration.evaluation_issue_provider.EvaluationIssueProvider.find", ctx.issue_provider.find)
    monkeypatch.setattr("src.agentauth.model_policy.ensure_snapshot_report_only", AsyncMock(return_value={"status": "available"}))

    async def authorization(db, **kwargs):
        from src.orchestration.execution_policy import AuthorizationContext

        return AuthorizationContext(
            policy=kwargs["policy"],
            accepted_plan_version=1,
            in_force_plan_version=1,
            principal_id="human",
            member_org_id=ctx.node.org_id,
            principal_can_authorize=True,
            now=datetime.now(UTC),
            credential_scope=kwargs["credential_scope"],
            observed_spend_usd=kwargs["spend"].total_usd,
            observed_attempts=kwargs["node"].attempts,
            observed_concurrency=await policy_admission._running_count(db, org_id=ctx.node.org_id, flow_id=ctx.flow.id),
            work_owned_by_policy_flow=True,
        )

    async def costs(*args, **kwargs):
        from src.orchestration.cost import CostStatus

        return [SimpleNamespace(address="cycle/E1/W1/N1", status=CostStatus.UNKNOWN if ctx.spend is None else CostStatus.KNOWN, amount_usd=ctx.spend)]

    monkeypatch.setattr(policy_admission, "resolve_authorization_context", authorization)
    monkeypatch.setattr(policy_admission, "get_cost_by_address", costs)
    monkeypatch.setattr(policy_admission, "release_flow_admission", AsyncMock())
    ctx.flow_reservation = AsyncMock(return_value=SimpleNamespace(admitted=True))
    monkeypatch.setattr(policy_admission, "reserve_flow_admission", ctx.flow_reservation)
    monkeypatch.setattr("src.orchestration.flow_meter.prepare_flow_meter", AsyncMock(return_value=True))
    async with ctx.factory() as db:
        report = await run_dispatch_pass(db, ctx.service.config)
        await db.commit()
        await prepare_pending(db, report, writer=ctx.service.writer)
    return report


async def test_correction_dispatch_inherits_protected_lineage_and_same_allowance(correction, monkeypatch):
    from src.agentauth.engine import validate_engine_authority
    from src.orchestration.models import OrchestrationWorkClaim
    from src.orchestration.runtime_policy import runtime_action

    ctx = correction
    await tick(ctx)
    await tick(ctx)
    report = await dispatch_correction(ctx, monkeypatch)
    assert report.dispatched == 1 and report.publish_failed == 0 and len(report.pending) == 1, report
    envelope = report.pending[0].envelope
    run = envelope["message_id"]
    raw = ctx.store._read(f"TENANT#{ctx.node.org_id}", f"EXEC#{run}")
    grant = ctx.store.live_grant(invocation_id=run, tenant_id=ctx.node.org_id, attempt=1, now=datetime.now(UTC))
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, report.pending[0].node_id)
        action = await correction_link(db, node)
        parent = ctx.store.live_grant(invocation_id=action.detail["parent_run_id"], tenant_id=ctx.node.org_id, attempt=1, now=datetime.now(UTC))
        claim = await db.scalar(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.issue_number == 71))
        assert node.attempts == 1 and runtime_action(raw, node) is Action.REPAIR
        assert claim.owner_ref == ctx.flow.id and claim.id != ctx.claim.id and claim.active_run_id == run
        assert grant.flow_id == parent.flow_id == ctx.flow.id
        assert grant.expires_at == parent.expires_at and grant.max_chain_depth == parent.max_chain_depth
        assert raw["parent_grant_id"]["S"] == parent.grant_id
        assert int(raw["chain_depth"]["N"]) == action.detail["chain_depth"]
        await validate_engine_authority(session=db, execution=raw, grant=grant, store=ctx.store)
        parent_node = await db.get(OrchestrationNode, ctx.eval_id)
        parent_node.state = "halted"
        await db.commit()
        from src.agentauth.bootstrap import BootstrapRefusedError

        with pytest.raises(BootstrapRefusedError, match="correction assignment"):
            await validate_engine_authority(session=db, execution=raw, grant=grant, store=ctx.store)
    reservation = ctx.flow_reservation.await_args.kwargs
    assert reservation["flow_id"] == ctx.flow.id and reservation["settled_usd"] == ctx.spend
    assert reservation["policy"].limits.max_spend_usd == ctx.policy.limits.max_spend_usd


@pytest.mark.parametrize("outcome", ["pass", "fail", "runtime_changed"])
async def test_correction_uses_review_merge_deploy_then_fresh_evaluation(correction, monkeypatch, outcome):
    from copy import copy
    from dataclasses import replace

    from src.orchestration.evaluation_plan import identity_for
    from src.orchestration.execution_store import load_execution
    from src.orchestration.handoff import commit_handoff
    from src.orchestration.models import OrchestrationExecution, OrchestrationWorkClaim
    from src.orchestration.pr_bindings import PullRequestIdentity, register_binding, resolve_registration_target
    from src.orchestration.state import ActorKind
    from tests.orchestration.test_deployment_controller import finish, prepare_runtime
    from tests.orchestration.test_deployment_workflows import prepare_deployment
    from tests.orchestration.test_evaluation_controller import evidence_rows, nodes
    from tests.orchestration.test_merge_controller import prepared_merge

    ctx = correction
    await tick(ctx)
    await tick(ctx)
    report = await dispatch_correction(ctx, monkeypatch)
    assert len(report.pending) == 1, report
    child = copy(ctx)
    child.root = report.pending[0].envelope["message_id"]
    child.head = "e" * 40
    async with ctx.factory() as db:
        child.node = await db.get(OrchestrationNode, report.pending[0].node_id)
        child.claim = await db.scalar(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.issue_number == 71))
        row = await db.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == child.node.id))
        child.identity = identity_for(row)
        child.execution = (await load_execution(db, identity=child.identity)).record
        target = await resolve_registration_target(db, run_id=child.root, expected_org_id=child.node.org_id)
        pr = PullRequestIdentity(123, "PR_correction", ctx.binding.repo, 78, child.head)
        child.binding, _ = await register_binding(db, target=target, pr=pr, actor_id=child.root, actor_kind=ActorKind.SERVICE)
        assert json.loads(child.binding.accepted_scope)["evaluation_correction"]["parent_node_id"] == ctx.eval_id
        await commit_handoff(db, identity=child.identity, now=datetime.now(UTC), next_check_at=datetime.now(UTC))
        await db.commit()
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_pr_identity", AsyncMock(return_value=pr))
    await ctx.finish(child.root)
    source = "9" * 40
    async with prepared_merge(child, monkeypatch, merge_sha=source):
        # These are the published R2, M2, D2 and D3 handlers, including actual
        # provider HTTP contracts. Only external credentials/runtime are fixtures.
        await prepare_deployment(child, source=source)
        await prepare_runtime(child)
        child.target = replace(child.target, evidence=ctx.target.evidence)
        await finish(child)
        ctx.evaluation_runtime.deployments = child.runtime_services
        del ctx.evaluation_runtime.verify
        ctx.failed_criterion = outcome == "fail"
        report = await tick(ctx)
        async with ctx.factory() as db:
            records = list(
                (
                    await db.scalars(
                        select(OrchestrationExecution).where(OrchestrationExecution.node_id == ctx.eval_id).order_by(OrchestrationExecution.cycle)
                    )
                ).all()
            )
            debug = [(r.cycle, r.phase, r.status, r.block_detail, r.progress_note) for r in records]
            assert len(records) == 2, (report, debug)
            assert records[0].status == "concluded" and records[1].claim_id == child.claim.id
        if outcome == "runtime_changed":
            from src.orchestration.review_cycle import CycleBlockedError

            child.targets.resolve.side_effect = CycleBlockedError("evaluation_runtime_revision_changed")
        await tick(ctx)
        states = await nodes(ctx)
        assert states[ctx.node.id] == "passed" and states[child.node.id] == "passed"
        assert states[ctx.gate_id] == "awaiting_gate" and states[ctx.next_id] == "pending"
        if outcome == "runtime_changed":
            assert states[ctx.eval_id] == "running" and len(await evidence_rows(ctx)) == 1
            return
        if outcome == "fail":
            assert states[ctx.eval_id] == "running"
            assert len(await evidence_rows(ctx)) == 2
            ctx.issue, ctx.issue_number = None, 72
            await tick(ctx)
            await tick(ctx)
            async with ctx.factory() as db:
                actions = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == CORRECTION_KIND))).all())
                assert len(actions) == 2
                latest = next(a for a in actions if a.detail["evaluation_cycle"] == 2)
                assert latest.detail["remaining_corrections"] == 6
                prior = next(a for a in actions if a.detail["evaluation_cycle"] == 1)
                assert latest.detail["chain_depth"] > prior.detail["chain_depth"]
                assert latest.detail["issue"]["number"] == 72
            return
        assert states[ctx.eval_id] == "passed", (states, debug)
        assert states[ctx.node.id] == "passed" and states[child.node.id] == "passed"
        assert states[ctx.gate_id] == "awaiting_gate" and states[ctx.next_id] == "pending"
        receipts = await evidence_rows(ctx)
        assert len(receipts) == 2
        assert sorted(bool(r.detail["required_failures"]) for r in receipts) == [False, True]
        await tick(ctx)
        assert len(await evidence_rows(ctx)) == 2
        ctx.issue_provider.create.assert_awaited_once()


@pytest.mark.parametrize("fault", ["unknown_spend", "spent", "limit", "plan", "claim", "halt", "refused", "revoked", "depth"])
async def test_correction_refuses_stale_or_exhausted_authority(correction, fault):
    from decimal import Decimal

    from src.orchestration.models import OrchestrationWorkClaim

    ctx = correction
    if fault in {"unknown_spend", "spent"}:
        ctx.spend = None if fault == "unknown_spend" else Decimal(25)
    elif fault in {"revoked", "depth"}:
        if fault == "revoked":
            row = ctx.store._read(f"TENANT#{ctx.node.org_id}", f"AUTHORITY#{ctx.approval.id}")
            row["status"] = {"S": "revoked"}
        else:
            async with ctx.factory() as db:
                claim = await db.get(OrchestrationWorkClaim, ctx.claim.id)
            row = ctx.store._read(f"TENANT#{ctx.node.org_id}", f"EXEC#{claim.active_run_id}")
            row["chain_depth"] = {"N": "8"}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=row)
    else:
        async with ctx.factory() as db:
            if fault in {"halt", "refused"}:
                node = await db.get(OrchestrationNode, ctx.eval_id)
                node.state = "halted" if fault == "halt" else "rejected_at_gate"
            elif fault == "claim":
                claim = await db.get(OrchestrationWorkClaim, ctx.claim.id)
                claim.generation += 1
            else:
                plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
                if fault == "plan":
                    plan.version += 1
                else:
                    doc = json.loads(json.dumps(plan.plan_document))
                    doc["execution_policy"]["limits"]["max_attempts_per_node"] = 1
                    plan.plan_document = doc
            await db.commit()
    await tick(ctx)
    ctx.issue_provider.create.assert_not_awaited()
    async with ctx.factory() as db:
        assert await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == CORRECTION_KIND)) is None
        assert (await db.get(OrchestrationNode, ctx.node.id)).state == "passed"


@pytest.mark.parametrize("fault", ["closed", "edited", "missing", "unknown_spend", "spent", "halt", "revoked"])
async def test_child_dispatch_refuses_without_new_authority_or_allowance(correction, monkeypatch, fault):
    from dataclasses import replace
    from decimal import Decimal

    from src.orchestration.review_cycle import CycleBlockedError

    ctx = correction
    await tick(ctx)
    await tick(ctx)
    if fault == "closed":
        ctx.issue = replace(ctx.issue, state="closed")
    elif fault == "missing":
        ctx.issue = None
    elif fault == "edited":
        ctx.issue_provider.find.side_effect = CycleBlockedError("evaluation_correction_issue_mismatch")
    elif fault in {"unknown_spend", "spent"}:
        ctx.spend = None if fault == "unknown_spend" else Decimal(25)
    elif fault == "halt":
        async with ctx.factory() as db:
            node = await db.get(OrchestrationNode, ctx.eval_id)
            node.state = "halted"
            await db.commit()
    else:
        row = ctx.store._read(f"TENANT#{ctx.node.org_id}", f"AUTHORITY#{ctx.approval.id}")
        row["status"] = {"S": "revoked"}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=row)
    report = await dispatch_correction(ctx, monkeypatch)
    assert not report.pending and report.errors == 0, report
    if fault != "revoked":
        assert report.dispatched == 0
        ctx.flow_reservation.assert_not_awaited()


async def test_unknown_creation_never_posts_again_and_readback_is_bounded(correction):
    from src.orchestration.execution_read import _action_view

    ctx = correction
    ctx.lost_issue_response = True
    await tick(ctx)
    ctx.issue = None
    for _ in range(3):
        await tick(ctx)
    ctx.issue_provider.create.assert_awaited_once()
    async with ctx.factory() as db:
        action = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == CORRECTION_KIND))
        summary = _action_view(action).evidence_summary
        assert summary["stage"] == "creation_unresolved" and summary["issue_number"] is None
        assert not {"claim", "grant", "source_scope", "parent_run_id"}.intersection(json.dumps(summary).split())
        assert set(summary) == {"evaluation_cycle", "remaining_corrections", "issue_number", "child_node_id", "retest_cycle", "stage"}


async def test_parallel_ticks_admit_one_correlated_correction(correction):
    import asyncio

    ctx = correction
    await asyncio.gather(tick(ctx), tick(ctx))
    await asyncio.gather(tick(ctx), tick(ctx))
    await tick(ctx)
    ctx.issue_provider.create.assert_awaited_once()
    async with ctx.factory() as db:
        actions = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == CORRECTION_KIND))).all())
        assert len(actions) == 1
        assert await db.get(OrchestrationNode, child_id(actions[0].operation_key)) is not None


async def test_timeout_before_creation_can_resume_with_the_same_operation(correction):
    ctx = correction
    create = ctx.issue_provider.create.side_effect
    ctx.issue_provider.create.side_effect = httpx.ConnectTimeout("before request")
    await tick(ctx)
    ctx.issue_provider.create.side_effect = create
    for _ in range(3):
        await tick(ctx)
    async with ctx.factory() as db:
        actions = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == CORRECTION_KIND))).all())
        assert len(actions) == 1 and actions[0].status == "succeeded"
        assert await db.get(OrchestrationNode, child_id(actions[0].operation_key)) is not None
