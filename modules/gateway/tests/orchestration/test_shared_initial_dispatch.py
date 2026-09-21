"""A fresh story in a continued legacy flow reaches review through real adapters."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from starlette.requests import Request

from src.agentauth import run_report_routes
from src.agentauth.pr_binding_routes import BindPullRequestRequest
from src.orchestration import shared_policy
from src.orchestration.adapters.github_comments import InputPath, apply_gate_answer_for_context
from src.orchestration.dispatch_pass import run_dispatch_pass
from src.orchestration.execution_state import ExecutionIdentity
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
)
from src.orchestration.pr_bindings import PullRequestIdentity
from src.orchestration.run_reports import OrchestrationRunReport, authenticate_run_report
from src.shared.models.base import Base
from tests.orchestration.test_review_cycle import tick
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401

ACTUAL_ACTIVE_COUNT = shared_policy._active_count


@pytest.mark.parametrize("flow_state", ["running", "pending"])
@pytest.mark.parametrize("later_gate", [False, True])
@pytest.mark.parametrize("recover_exited", [False, True])
async def test_fresh_story_dispatch_model_handoff_and_review(shared, monkeypatch, flow_state, later_gate, recover_exited):  # noqa: F811
    ctx = shared
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "false")
    monkeypatch.setenv("ADP_SHARED_RUN_REPORTING_ENABLED", "true")
    monkeypatch.setattr(shared_policy, "_active_count", ACTUAL_ACTIVE_COUNT)
    monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=123))
    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=42))
    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", AsyncMock(return_value=42))
    monkeypatch.setattr("src.shared.identity.resolver.resolve_root_user_entity_id", AsyncMock(return_value="human"))
    engine = ctx.factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=[OrchestrationEdge.__table__]))
    async with ctx.factory() as db:
        flow = await db.get(OrchestrationFlow, ctx.flow.id)
        flow.state = flow_state
        fresh = OrchestrationNode(
            org_id=ctx.node.org_id,
            flow_id=ctx.flow.id,
            epic_ref="E1",
            wave_ref="W2",
            node_ref="N2",
            kind="story",
            state="ready",
            title="New remaining story",
            issue_ref="44",
            attempts=0,
        )
        db.add(fresh)
        if later_gate:
            gate = OrchestrationNode(
                org_id=ctx.node.org_id,
                flow_id=ctx.flow.id,
                epic_ref="E1",
                wave_ref="W2",
                node_ref="accept",
                kind="gate",
                state="awaiting_gate",
                title="Accept delivery under the current plan",
                attempts=0,
            )
            db.add(gate)
            await db.flush()
            plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
            access = SimpleNamespace(get_user_role=AsyncMock(return_value=(SimpleNamespace(value="owner"), None, None)), check_permission=AsyncMock())
            outcome = await apply_gate_answer_for_context(
                db,
                context=SimpleNamespace(org_id=ctx.node.org_id, user_id="human"),
                node_id=gate.id,
                approve=True,
                reason="Accept the exact current plan before initial dispatch",
                access=access,
                input_path=InputPath.DASHBOARD,
                expected_plan_hash=plan.plan_hash,
            )
            assert outcome.status.value == "applied"
            assert outcome.decision_id != plan.accepted_by_decision_id
        await db.commit()
    async with ctx.factory() as db:
        report = await run_dispatch_pass(db, config=ctx.service.config)
        assert report.dispatched == 1, report
        assert len(report.pending) == 1
        envelope = report.pending[0].envelope
        assert envelope["action"] == "develop" and envelope["pr_binding_required"]
        assert not envelope.get("handoff_required")
        assert envelope["orchestration"]["node_id"] == fresh.id
        assert envelope["orchestration"]["attempt"] == 1
        assert envelope["actor"]["user_id"] == "human"
        assert envelope["run_report"]["credential"]
        if later_gate:
            assert envelope["orchestration"]["root_decision_id"] == outcome.decision_id
        await db.commit()
    if later_gate:
        # Existing failed /started reports recover through their same committed
        # assignment; outbox replay cannot manufacture another attempt or root.
        async with ctx.factory() as db:
            row = await db.get(OrchestrationRunReport, envelope["message_id"])
            row.block_code = "execution_assignment_unverifiable"
            await db.commit()
        async with ctx.factory() as db:
            replay = await run_dispatch_pass(db, config=ctx.service.config)
            assert len(replay.pending) == 1 and replay.pending[0].envelope == envelope
            row = await db.get(OrchestrationRunReport, envelope["message_id"])
            assert row.block_code is None and row.retryable is False
            assert (await db.get(OrchestrationNode, fresh.id)).attempts == 1
            # Exercise acknowledgement recovery independently of replay clearing.
            row.block_code = "execution_assignment_unverifiable"
            await db.commit()
    monkeypatch.setattr(run_report_routes, "_sessions", lambda: ctx.factory)
    request = Request({"type": "http", "headers": [(b"x-adp-report-credential", envelope["run_report"]["credential"].encode())]})
    started = await run_report_routes.worker_started(run_report_routes.WorkerStarted(ownership_nonce="a" * 32), request)
    assert started["worker_receipt"]["run_id"] == envelope["message_id"]
    assert started["block_code"] is None and started["retryable"] is False
    async with ctx.factory() as db:
        assignment = await authenticate_run_report(db, envelope["run_report"]["credential"])
        policy, principal, node, flow = await shared_policy.authorize_shared_model(db, assignment)
        assert policy.principal_id == principal == "human" and node.id == fresh.id and flow.state == flow_state
        execution = await db.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == fresh.id))
    new_head = "d" * 40
    monkeypatch.setattr(
        "src.orchestration.pr_identity.resolve_pr_identity",
        AsyncMock(return_value=PullRequestIdentity(123, "PR_new", ctx.binding.repo, 88, new_head)),
    )
    receipt = await run_report_routes.register_pull_request(
        BindPullRequestRequest(repo=ctx.binding.repo, pr_number=88, provider_repository_id=123, provider_pr_node_id="PR_new", head_sha=new_head),
        request,
    )
    assert receipt["binding_receipt"]["bound"]
    if not recover_exited:
        finished = await run_report_routes.terminal_report(run_report_routes.TerminalReport(outcome="complete"), request)
        assert finished["terminal_receipt"]["outcome"] == "complete"
    async with ctx.factory() as db:
        ctx.node = await db.get(OrchestrationNode, fresh.id)
        ctx.binding = await db.scalar(select(OrchestrationPullRequestBinding).where(OrchestrationPullRequestBinding.node_id == fresh.id))
    ctx.root = envelope["message_id"]
    ctx.head = new_head
    ctx.identity = ExecutionIdentity(ctx.node.org_id, fresh.id, 1, execution.accepted_plan_version, execution.claim_id, execution.claim_generation)
    ctx.execution = SimpleNamespace(id=execution.id)
    if recover_exited:
        from src.orchestration.review_recovery import ReviewRecoveryRequest, request_review_recovery
        from src.orchestration.stall import _continuation_clock

        resolver = SimpleNamespace(
            read_current=AsyncMock(return_value={"tenant_id": ctx.node.org_id, "status": "failed", "arrived_at": "2026-01-01T00:00:00Z"})
        )
        monkeypatch.setattr("src.orchestration.controls.get_run_binding_resolver", AsyncMock(return_value=resolver))
        body = ReviewRecoveryRequest(
            expected_attempt=1,
            expected_plan_version=ctx.identity.accepted_plan_version,
            expected_run_id=ctx.root,
            expected_head_sha=new_head,
            reason="Fresh review after verified developer exit with retained PR and missing terminal.",
        )
        async with ctx.factory() as db:
            (await db.get(OrchestrationNode, ctx.node.id)).state = "failed"
            await db.commit()
        async with ctx.factory() as db:
            preview = await request_review_recovery(
                db, org_id=ctx.node.org_id, node_id=ctx.node.id, actor_id="human", actor_role="owner", request=body
            )
            body.expected_snapshot = preview["snapshot"]
        async with ctx.factory() as db:
            await request_review_recovery(
                db, org_id=ctx.node.org_id, node_id=ctx.node.id, actor_id="human", actor_role="owner", request=body, accept=True
            )
            await db.commit()
        async with ctx.factory() as db:
            managed, since = await _continuation_clock(
                db, SimpleNamespace(org_id=ctx.node.org_id, flow_id=ctx.flow.id, node_id=ctx.node.id, attempts=1)
            )
            assert managed and since is not None
    tick_result = await tick(ctx)
    async with ctx.factory() as db:
        observed_execution = await db.get(OrchestrationExecution, execution.id)
    assert tick_result.effects_succeeded == 1, (tick_result, vars(observed_execution))
    if recover_exited:
        async with ctx.factory() as db:
            prior = await db.get(OrchestrationRunReport, ctx.root)
            assert prior.terminal_receipt is None
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as stale:
            await run_report_routes.terminal_report(run_report_routes.TerminalReport(outcome="complete"), request)
        assert stale.value.status_code == 404
    assert ctx.calls[-1]["persona"] == "agent-codex-reviewer"
    assert ctx.calls[-1]["review_expect"]["author_run_id"] == envelope["message_id"]
    assert ctx.calls[-1]["review_expect"]["expected_head_sha"] == new_head
