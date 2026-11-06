"""Ready machine evaluations must honor inputs added by exact-revision approval."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.accepted_node import unresolved_required_inputs
from src.orchestration.compile import plan_hash
from src.orchestration.dispatch import graph_address
from src.orchestration.dispatch_pass import run_dispatch_pass
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationEdge, OrchestrationExecution, OrchestrationNode
from src.orchestration.pending_amendments import accept_amendment, register_amendment_draft
from src.orchestration.proposal import LoopProposal, NamedInputPrerequisite, ProposedEdge, ProposedNode
from src.orchestration.repository_evaluation import observe_repository_evaluation
from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.tick import run_tick
from src.shared.models.base import Base
from tests.orchestration import test_repository_evaluation as evaluation_tests
from tests.orchestration.test_amendment_review import open_request
from tests.orchestration.test_pending_amendments import amender
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401

repository_evaluation = evaluation_tests.repository_evaluation


async def input_gate(ctx, session):
    gate = OrchestrationNode(
        org_id=ctx.node.org_id,
        flow_id=ctx.flow.id,
        epic_ref="E1",
        wave_ref="inputs",
        node_ref="target",
        kind="gate",
        title="Authorized target",
        state="passed",
        attempts=0,
    )
    session.add(gate)
    await session.flush()
    plan = await session.get(OrchestrationAcceptedPlan, ctx.plan.id)
    document = deepcopy(plan.plan_document)
    document["nodes"][-1]["required_inputs"] = [dict(name=gate.title, source="node:" + graph_address(gate, flow_slug="cycle"))]
    document["nodes"].append(dict(address=graph_address(gate, flow_slug="cycle"), kind=gate.kind, title=gate.title))
    plan.plan_document = document
    await session.commit()
    return gate


async def withdraw_input(ctx, gate_id):
    async with ctx.factory() as session:
        gate = await session.get(OrchestrationNode, gate_id)
        gate.state = "pending"
        await session.commit()


def withdraw_on_reauthorization(ctx, gate_id):
    async def token(*args, **kwargs):
        if ctx.provider.token.await_count == 2:
            await withdraw_input(ctx, gate_id)
        return "test-scoped-token"

    ctx.provider.token.reset_mock()
    ctx.provider.token.side_effect = token


@pytest.mark.parametrize("withdraw_at", ["observation", "authorization"])
async def test_observation_rechecks_input_after_provider_read(repository_evaluation, withdraw_at):
    ctx = repository_evaluation
    async with ctx.factory() as session:
        gate = await input_gate(ctx, session)
        node = await session.get(OrchestrationNode, ctx.eval_id)
        observe = ctx.provider.observe.side_effect

        async def revoke_during_read(*args):
            await withdraw_input(ctx, gate.id)
            return await observe(*args)

        if withdraw_at == "observation":
            ctx.provider.observe.side_effect = revoke_during_read
        else:
            withdraw_on_reauthorization(ctx, gate.id)
        assert await observe_repository_evaluation(session, node, provider=ctx.provider)
        await session.commit()
        assert node.state == "ready" and node.attempts == 0
        reasons = list(await session.scalars(select(OrchestrationDecision.reason).where(OrchestrationDecision.node_id == node.id)))
        assert len(reasons) == 1
        assert json.loads(reasons[0])["block_code"] == "required_inputs_unavailable"
        assert "node:cycle/E1/inputs/target" in json.loads(reasons[0])["required_input"]


async def amend_input(ctx, *, available=False):
    """Complete the legacy fixture graph/history before exercising real draft approval."""
    async with ctx.factory() as session:
        plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.superseded_at.is_(None)))
        if "flow_slug" not in plan.plan_document:
            connection = await session.connection()
            await connection.run_sync(Base.metadata.create_all)
            for execution in await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.flow_id == ctx.flow.id)):
                execution.status = "concluded"
                execution.pending_action_key = None
            for report in await session.scalars(select(OrchestrationRunReport).where(OrchestrationRunReport.flow_id == ctx.flow.id)):
                report.terminal_receipt = dict(
                    contract_version=1, run_id=report.run_id, attempt=report.attempt, outcome="complete", recorded_at=datetime.now(UTC).isoformat()
                )
            rows = list(await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == ctx.flow.id)))
            addresses = {node.id: graph_address(node, flow_slug="cycle") for node in rows}
            document = deepcopy(plan.plan_document)
            document.pop("execution_continuation", None)
            document.update(
                flow_slug="cycle",
                title="Amended evaluation",
                org_id=ctx.node.org_id,
                spec_revision="5329-ready-input",
                nodes=[
                    ProposedNode(
                        address=addresses[node.id],
                        kind=node.kind,
                        title=node.title,
                        issue_ref=node.issue_ref,
                        evaluation=ctx.spec if node.id == ctx.eval_id else None,
                    ).model_dump(mode="json")
                    for node in rows
                ],
                edges=[
                    ProposedEdge(from_address=addresses[edge.from_node_id], to_address=addresses[edge.to_node_id]).model_dump()
                    for edge in await session.scalars(select(OrchestrationEdge).where(OrchestrationEdge.flow_id == ctx.flow.id))
                ],
            )
            proposal = LoopProposal.model_validate(document)
            plan.plan_document = proposal.model_dump(mode="json")
            plan.plan_hash = plan_hash(proposal)
            await session.commit()
        proposal = LoopProposal.model_validate(plan.plan_document)
        proposal.execution_policy = proposal.execution_policy.model_copy(update=dict(policy_id=None, policy_hash=None, principal_id=None))
        evaluation = next(node for node in proposal.nodes if node.kind == "eval")
        evaluation.required_inputs = [NamedInputPrerequisite(name="Authorized target", source="environment:withheld-target")]
        if available:
            proposal.execution_policy.environment_connection_ids.append("withheld-target")
        version, base_hash = plan.version, plan.plan_hash
        request = await open_request(session, ctx.flow.id, org_id=ctx.node.org_id, decision_id=f"inputs-{version}", run_id=f"author-{version}")
        draft = await register_amendment_draft(
            session, org_id=ctx.node.org_id, request=request, author_run_id=request.author_run_id, proposal=proposal
        )
        await session.commit()
        result = await accept_amendment(
            session,
            draft_id=draft.draft_id,
            actor=amender(ctx.node.org_id, actor_id="human"),
            flow_id=ctx.flow.id,
            expected_proposal_hash=draft.proposal_hash,
            expected_base_plan_version=version,
            expected_base_plan_hash=base_hash,
        )
        await session.commit()
        node = await session.get(OrchestrationNode, ctx.eval_id)
        assert node.state == "ready" and node.attempts == 0
        assert len(await unresolved_required_inputs(session, node)) == (0 if available else 1)
        return result


@pytest.mark.parametrize("entry_point", ["dispatch", "observer"])
async def test_ready_evaluation_honors_amended_inputs_until_authorized(repository_evaluation, monkeypatch, entry_point):
    from src.orchestration import shared_policy

    ctx = repository_evaluation
    original_authorization = shared_policy.resolve_authorization_context

    async def authorization(session, **kwargs):
        context = await original_authorization(session, **kwargs)
        return replace(context, accepted_plan_version=kwargs["plan_version"], in_force_plan_version=kwargs["plan_version"])

    monkeypatch.setattr(shared_policy, "resolve_authorization_context", authorization)
    monkeypatch.setattr("src.orchestration.repository_evaluation.RepositoryEvidenceProvider", lambda: ctx.provider)
    monkeypatch.setattr("src.orchestration.report_dispatch.recover_pending_reports", AsyncMock())
    await amend_input(ctx)
    for _ in range(3):
        async with ctx.factory() as session:
            node = await session.get(OrchestrationNode, ctx.eval_id)
            if entry_point == "dispatch":
                assert (await run_tick(session)).success
                report = await run_dispatch_pass(session)
                assert report.success and not report.pending
                assert not report._repository_evaluation_attempted
            else:
                assert await observe_repository_evaluation(session, node, provider=ctx.provider)
            await session.commit()
            assert node.state == "ready" and node.attempts == 0
            decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id)))
            blockers = [json.loads(row.reason) for row in decisions if row.actor_id == "system:repository-evaluation"]
            assert len(blockers) == 1
            assert blockers[0]["block_code"] == "required_inputs_unavailable"
            assert "Authorized target" in blockers[0]["required_input"]
            assert "environment:withheld-target" in blockers[0]["required_input"]
    ctx.provider.token.assert_not_awaited()
    ctx.provider.observe.assert_not_awaited()
    ctx.claim_spy.assert_not_awaited()
    await amend_input(ctx, available=True)
    async with ctx.factory() as session:
        node = await session.get(OrchestrationNode, ctx.eval_id)
        assert node.state == "ready"
        if entry_point == "dispatch":
            assert (await run_tick(session)).success
            assert (await run_dispatch_pass(session)).success
        else:
            assert await observe_repository_evaluation(session, node, provider=ctx.provider)
        await session.commit()
        assert node.state == "passed" and node.attempts == 1
    ctx.provider.observe.assert_awaited_once()
    ctx.claim_spy.assert_not_awaited()
