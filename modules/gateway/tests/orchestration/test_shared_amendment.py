"""Appending prerequisites preserves accepted authority and settled delivery history."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import JSON, select

from src.orchestration.compile import compile_proposal
from src.orchestration.continuation import digest
from src.orchestration.execution_policy import ExecutionPolicy, stamp_policy
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationAmendmentRequest,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.shared_amendment import SharedAppendError, SharedAppendRequest, accept_shared_append, preview_shared_append
from src.orchestration.state import ActorKind
from tests.orchestration.test_amend import address, approval, session, valid_proposal  # noqa: F401

ROLE = "arn:aws:iam::123456789012:role/worker"


@pytest.fixture
async def appendable(session, approval, monkeypatch):  # noqa: F811
    result = await compile_proposal(session, valid_proposal(), approval)
    plan = await session.scalar(select(OrchestrationAcceptedPlan))
    now = datetime.now(UTC)
    policy = stamp_policy(
        ExecutionPolicy.model_validate(
            dict(
                schema_version=2,
                org_id=approval.org_id,
                repository_ids=["org/repo"],
                allowed_actions=["develop", "review", "repair", "merge"],
                expires_at=now + timedelta(hours=2),
                limits=dict(max_spend_usd="100", max_attempts_per_node=3, max_concurrent_actions=1, max_wall_clock_seconds=3600),
                user_credentials=dict(
                    permission_mode="user_configured",
                    lifetime="provider_managed",
                    aws_role_arns=[ROLE],
                    actions=["develop", "review", "repair", "merge"],
                ),
            )
        ),
        principal_id=approval.actor_id,
        org_id=approval.org_id,
    )
    document = deepcopy(plan.plan_document)
    document["execution_policy"] = policy.model_dump(mode="json")
    document["execution_continuation"] = dict(
        contract_version=1,
        mode="shared_worker_role",
        budget_scope="authenticated_gateway_calls",
        worker_role_arn=ROLE,
        accepted_at=(now - timedelta(minutes=20)).isoformat(),
        prior_spend_usd="12.34",
        prior_attempts={},
        initial_runs={},
        delivery_mode="code_only",
    )
    plan.plan_document, plan.plan_hash = document, digest(document)
    flow = await session.get(OrchestrationFlow, result.flow_id)
    flow.state = "running"
    await session.commit()
    monkeypatch.setenv("ADP_SHARED_WORKER_CONTINUATION_ENABLED", "true")
    monkeypatch.setenv("AGENT_WORKER_ROLE_ARN", ROLE)
    meter = AsyncMock(return_value=SimpleNamespace(total_usd=Decimal("12.34")))
    monkeypatch.setattr("src.orchestration.shared_amendment.read_flow_meter", meter)
    request = SharedAppendRequest(
        expected_plan_version=plan.version,
        expected_plan_hash=plan.plan_hash,
        added_nodes=[dict(address=address("producer"), kind="story", title="Complete prerequisite", issue_ref="5329")],
        added_edges=[dict(from_address=address("producer"), to_address=address("eval"))],
        reason="Add the missing producer within the existing accepted allowance",
    )
    return SimpleNamespace(actor=approval, flow=flow, plan=plan, original=deepcopy(document), nodes=result.node_ids, request=request, meter=meter)


async def settled_history(db, ctx):
    node = await db.get(OrchestrationNode, ctx.nodes[address("story-a")])
    node.state, node.attempts = "passed", 2
    run_id = "orch:settled:2"
    claim = OrchestrationWorkClaim(
        org_id=ctx.actor.org_id,
        provider_repository_id=123,
        issue_number=4196,
        owner_kind="engine_flow",
        owner_ref=ctx.flow.id,
        state="held",
        generation=1,
        active_run_id=run_id,
        claim_event_id=run_id,
    )
    db.add(claim)
    await db.flush()
    execution = OrchestrationExecution(
        org_id=ctx.actor.org_id,
        flow_id=ctx.flow.id,
        node_id=node.id,
        cycle=2,
        phase="completed",
        status="concluded",
        accepted_plan_version=ctx.plan.version,
        claim_id=claim.id,
        claim_generation=claim.generation,
        revision=7,
        attempts=2,
    )
    binding = OrchestrationPullRequestBinding(
        org_id=ctx.actor.org_id,
        flow_id=ctx.flow.id,
        node_id=node.id,
        attempt=2,
        run_id=run_id,
        repo="org/repo",
        pr_number=50,
        provider_repository_id=123,
        provider_pr_node_id="PR_50",
        installation_id=42,
        head_sha="a" * 40,
        revision=3,
        role="implementation",
        state="active",
        registered_by=ctx.actor.actor_id,
        registered_by_kind="human",
        accepted_scope="unchanged",
    )
    report = OrchestrationRunReport(
        run_id=run_id,
        credential_hash="b" * 64,
        org_id=ctx.actor.org_id,
        flow_id=ctx.flow.id,
        node_id=node.id,
        attempt=2,
        persona="developer",
        repo="org/repo",
        installation_id=42,
        dispatch_metadata={},
        expires_at=datetime.now(UTC) + timedelta(days=1),
        terminal_receipt=dict(contract_version=1, run_id=run_id, attempt=2, outcome="complete", recorded_at=datetime.now(UTC).isoformat()),
    )
    db.add_all([execution, binding, report])
    await db.flush()
    action = OrchestrationAction(org_id=ctx.actor.org_id, execution_id=execution.id, operation_key="merge:50", kind="merge", status="succeeded")
    db.add(action)
    await db.commit()
    return SimpleNamespace(node=node, execution=execution, report=report, claim=claim, binding=binding, action=action)


async def preview(db, ctx, request=None):
    return await preview_shared_append(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request or ctx.request)


async def accept(db, ctx, request=None):
    request = request or ctx.request
    result = await preview(db, ctx, request)
    request = request.model_copy(update={"expected_snapshot": result["snapshot"]})
    return await accept_shared_append(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request), request


async def test_append_preserves_policy_clock_nodes_and_settled_history(session, appendable):  # noqa: F811
    ctx = appendable
    history = await settled_history(session, ctx)
    before = (history.execution.accepted_plan_version, history.execution.revision, history.binding.revision, history.report.terminal_receipt)
    result, request = await accept(session, ctx)
    await session.commit()
    current = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.superseded_at.is_(None)))
    assert current.version == 2 and result["nodes_created"] == result["edges_created"] == 1
    assert current.plan_document["execution_policy"] == ctx.original["execution_policy"]
    assert current.plan_document["execution_continuation"] == ctx.original["execution_continuation"]
    assert ctx.plan.plan_document == ctx.original and ctx.plan.superseded_at is not None
    assert (history.execution.accepted_plan_version, history.execution.revision, history.binding.revision, history.report.terminal_receipt) == before
    for old_address, node_id in ctx.nodes.items():
        assert await session.get(OrchestrationNode, node_id) is not None
    assert history.node.state == "passed" and history.node.attempts == 2
    # The original response may be lost after workers start on the new plan.
    new = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.node_ref == "producer"))
    assert new.state == "pending" and new.attempts == 0
    new.state, new.attempts = "running", 1
    await session.commit()
    replay = await accept_shared_append(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
    assert not replay["created"] and replay["decision_id"] == result["decision_id"]
    assert new.state == "running" and new.attempts == 1
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 2


@pytest.mark.parametrize("case", ["node", "execution", "effect", "report_null", "report_json_null", "claim", "authoring"])
async def test_unfinished_work_blocks_plan_version_change(session, appendable, case):  # noqa: F811
    ctx = appendable
    state = await settled_history(session, ctx)
    if case == "node":
        state.node.state = "awaiting_merge"
    elif case == "execution":
        state.execution.status = "blocked"
    elif case == "effect":
        state.action.status = "unknown"
    elif case.startswith("report"):
        state.report.terminal_receipt = JSON.NULL if case == "report_json_null" else None
    elif case == "claim":
        state.claim.active_run_id = "unverified-worker"
    else:
        session.add(
            OrchestrationAmendmentRequest(
                org_id=ctx.actor.org_id,
                flow_id=ctx.flow.id,
                replan_decision_id=ctx.plan.accepted_by_decision_id,
                requested_by=ctx.actor.actor_id,
                request_text="Unfinished replan",
                state="queued",
            )
        )
    await session.commit()
    with pytest.raises(SharedAppendError):
        await preview(session, ctx)
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1


@pytest.mark.parametrize("case", ["existing_node", "kind", "new_wave", "cycle", "started_target", "service", "different_owner", "stale_snapshot"])
async def test_append_refuses_scope_changes_and_stale_preview(session, appendable, case):  # noqa: F811
    ctx = appendable
    request = ctx.request
    data = request.model_dump(mode="json")
    if case == "existing_node":
        data["added_nodes"][0]["address"] = address("story-a")
    elif case == "kind":
        data["added_nodes"][0]["kind"] = "gate"
    elif case == "new_wave":
        data["added_nodes"][0]["address"] = address("producer", wave="new-wave")
    elif case == "cycle":
        data["added_edges"].append(dict(from_address=address("eval"), to_address=address("producer")))
    elif case == "started_target":
        node = await session.get(OrchestrationNode, ctx.nodes[address("eval")])
        node.state, node.attempts = "passed", 1
    elif case == "service":
        ctx.actor = replace(ctx.actor, actor_kind=ActorKind.SERVICE)
    elif case == "different_owner":
        ctx.actor = replace(ctx.actor, actor_id="another-human")
    else:
        old = await preview(session, ctx)
        data["expected_snapshot"] = old["snapshot"]
        node = await session.get(OrchestrationNode, ctx.nodes[address("story-b")])
        node.state = "ready"
    request = SharedAppendRequest.model_validate(data)
    await session.commit()
    with pytest.raises(SharedAppendError):
        if case == "stale_snapshot":
            await accept_shared_append(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
        else:
            await preview(session, ctx, request)
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1


async def test_partial_append_rolls_back_all_new_nodes_and_edges(session, appendable, monkeypatch):  # noqa: F811
    ctx = appendable
    expected = await preview(session, ctx)
    request = ctx.request.model_copy(update={"expected_snapshot": expected["snapshot"]})
    monkeypatch.setattr("src.orchestration.shared_amendment.upsert_edges", AsyncMock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        await accept_shared_append(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
    assert len(list(await session.scalars(select(OrchestrationNode)))) == len(ctx.nodes)
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1


async def test_generic_amendment_cannot_strip_shared_authority(session, appendable):  # noqa: F811
    from src.orchestration.amend import AmendmentContext, amend_plan
    from src.orchestration.compile import ProposalRejectedError

    ctx = appendable
    actor = AmendmentContext(org_id=ctx.actor.org_id, actor_id=ctx.actor.actor_id, actor_role=ctx.actor.actor_role)
    with pytest.raises(ProposalRejectedError, match="bounded append"):
        await amend_plan(session, ctx.flow.id, valid_proposal(), actor)
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1


async def test_immutable_allowance_and_evaluation_attachment_are_visible_in_preview(session, appendable):  # noqa: F811
    ctx = appendable
    acceptance = OrchestrationDecision(
        org_id=ctx.actor.org_id,
        flow_id=ctx.flow.id,
        node_id=ctx.nodes[address("eval")],
        kind="evaluation_contract_accepted",
        actor_id=ctx.actor.actor_id,
        actor_kind="human",
        actor_role=ctx.actor.actor_role,
        reason='{"plan_id":"' + ctx.plan.id + '"}',
    )
    session.add(acceptance)
    await session.commit()
    result = await preview(session, ctx)
    assert result["original_accepted_at"] == ctx.original["execution_continuation"]["accepted_at"]
    assert result["remaining_spend_usd"] == "87.66"
    assert result["evaluation_contracts_requiring_reacceptance"] == [acceptance.id]
    assert result["budget_meter_unchanged"] and result["existing_node_ids_unchanged"]
