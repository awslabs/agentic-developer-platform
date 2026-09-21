"""A fresh story in a continued legacy flow reaches review through real adapters."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from starlette.requests import Request

from src.agentauth import run_report_routes
from src.agentauth.pr_binding_routes import BindPullRequestRequest
from src.orchestration import shared_policy
from src.orchestration.dispatch_pass import run_dispatch_pass
from src.orchestration.execution_state import ExecutionIdentity
from src.orchestration.models import OrchestrationEdge, OrchestrationExecution, OrchestrationFlow, OrchestrationNode, OrchestrationPullRequestBinding
from src.orchestration.pr_bindings import PullRequestIdentity
from src.orchestration.run_reports import authenticate_run_report
from src.shared.models.base import Base
from tests.orchestration.test_review_cycle import tick
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401

ACTUAL_ACTIVE_COUNT = shared_policy._active_count


@pytest.mark.parametrize("flow_state", ["running", "pending"])
async def test_fresh_story_dispatch_model_handoff_and_review(shared, monkeypatch, flow_state):  # noqa: F811
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
        await db.commit()
    monkeypatch.setattr(run_report_routes, "_sessions", lambda: ctx.factory)
    request = Request({"type": "http", "headers": [(b"x-adp-report-credential", envelope["run_report"]["credential"].encode())]})
    started = await run_report_routes.worker_started(run_report_routes.WorkerStarted(ownership_nonce="a" * 32), request)
    assert started["worker_receipt"]["run_id"] == envelope["message_id"]
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
    finished = await run_report_routes.terminal_report(run_report_routes.TerminalReport(outcome="complete"), request)
    assert finished["terminal_receipt"]["outcome"] == "complete"
    async with ctx.factory() as db:
        ctx.node = await db.get(OrchestrationNode, fresh.id)
        ctx.binding = await db.scalar(select(OrchestrationPullRequestBinding).where(OrchestrationPullRequestBinding.node_id == fresh.id))
    ctx.root = envelope["message_id"]
    ctx.head = new_head
    ctx.identity = ExecutionIdentity(ctx.node.org_id, fresh.id, 1, execution.accepted_plan_version, execution.claim_id, execution.claim_generation)
    ctx.execution = SimpleNamespace(id=execution.id)
    assert (await tick(ctx)).effects_succeeded == 1
    assert ctx.calls[-1]["persona"] == "reviewer"
    assert ctx.calls[-1]["review_expect"]["author_run_id"] == envelope["message_id"]
    assert ctx.calls[-1]["review_expect"]["expected_head_sha"] == new_head
