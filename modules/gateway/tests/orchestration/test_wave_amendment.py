"""Paused amendments preserve active identities and lock entire started waves."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.continuation import digest
from src.orchestration.execution_state import ExecutionIdentity, OutcomeKind
from src.orchestration.execution_store import load_execution
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationEdge, OrchestrationNode
from src.orchestration.plan_lineage import ancestor_plan, receipt_plan
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.shared_amendment import SharedAppendError
from src.orchestration.state import ActorKind
from src.orchestration.wave_amendment import WaveDependencyRequest, accept_wave_dependencies, preview_wave_dependencies
from tests.orchestration.test_shared_amendment import address, appendable, approval, session, settled_history  # noqa: F401


@pytest.fixture
async def paused(session, appendable):  # noqa: F811
    ctx = appendable
    ctx.flow.execution_paused = True
    ctx.request = WaveDependencyRequest(
        expected_plan_version=ctx.plan.version,
        expected_plan_hash=ctx.plan.plan_hash,
        added_edges=[dict(from_address=address("story-a"), to_address=address("gate", wave="wave-2"))],
        reason="Record explicit prerequisite for the unstarted future wave",
    )
    await session.commit()
    return ctx


async def preview(db, ctx, request=None):
    return await preview_wave_dependencies(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request or ctx.request)


async def accept(db, ctx, request=None):
    request = request or ctx.request
    result = await preview(db, ctx, request)
    request = request.model_copy(update={"expected_snapshot": result["snapshot"]})
    accepted = await accept_wave_dependencies(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
    return accepted, request


async def test_active_worker_survives_amendment_and_ready_is_recomputed(session, paused):  # noqa: F811
    ctx = paused
    saved = await settled_history(session, ctx)
    saved.node.state, saved.execution.status, saved.execution.phase = "running", "awaiting_external", "delivering"
    saved.report.terminal_receipt = None
    saved.action.status = "pending"
    future = await session.get(OrchestrationNode, ctx.nodes[address("gate", wave="wave-2")])
    future.state = "ready"
    await session.commit()
    identity = ExecutionIdentity(
        org_id=ctx.actor.org_id, node_id=saved.node.id, cycle=2, accepted_plan_version=1, claim_id=saved.claim.id, claim_generation=1
    )
    before = (saved.execution.revision, saved.binding.revision, saved.claim.active_run_id, saved.report.dispatch_metadata)
    result, request = await accept(session, ctx)
    await session.commit()
    current = await OrchestrationRepository(session).get_accepted_plan(org_id=ctx.actor.org_id, flow_id=ctx.flow.id)
    assert result["plan_version"] == 2 and ctx.flow.execution_paused
    assert current.plan_document["execution_policy"] == ctx.original["execution_policy"]
    assert current.plan_document["execution_continuation"] == ctx.original["execution_continuation"]
    assert current.plan_document["nodes"] == ctx.original["nodes"]
    assert before == (saved.execution.revision, saved.binding.revision, saved.claim.active_run_id, saved.report.dispatch_metadata)
    assert saved.node.state == "running" and saved.execution.accepted_plan_version == 1 and future.state == "pending"
    outcome = await load_execution(session, identity=identity)
    assert outcome.kind is OutcomeKind.APPLIED
    assert await ancestor_plan(session, current, 1, node_id=saved.node.id) is ctx.plan
    assert await ancestor_plan(session, current, 1, node_id=future.id) is None
    # Retrying a lost response after resume is read-only and never resets work.
    ctx.flow.execution_paused = False
    future.state = "running"
    future.attempts = 1
    await session.commit()
    replay = await accept_wave_dependencies(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
    assert not replay["created"] and replay["decision_id"] == result["decision_id"]
    assert future.state == "running" and future.attempts == 1


@pytest.mark.parametrize("evidence", ["node", "execution", "report", "binding", "claim", "dispatch", "past_state", "waiver"])
async def test_history_freezes_unstarted_siblings_even_after_counter_reset(session, paused, evidence):  # noqa: F811
    ctx = paused
    saved = await settled_history(session, ctx)
    saved.node.state, saved.node.attempts = "pending", 0
    if evidence != "execution":
        await session.delete(saved.action)
        await session.delete(saved.execution)
    if evidence != "report":
        await session.delete(saved.report)
    if evidence != "binding":
        await session.delete(saved.binding)
    if evidence != "claim":
        await session.delete(saved.claim)
    if evidence == "node":
        saved.node.attempts = 1
    elif evidence in {"dispatch", "past_state", "waiver"}:
        session.add(
            OrchestrationDecision(
                org_id=ctx.actor.org_id,
                flow_id=ctx.flow.id,
                node_id=saved.node.id,
                kind={"dispatch": "agent_dispatched", "past_state": "node_transition", "waiver": "evaluation_waived"}[evidence],
                actor_id=ctx.actor.actor_id,
                actor_role=ctx.actor.actor_role,
                actor_kind="human",
                from_state="running" if evidence == "past_state" else None,
            )
        )
    request = ctx.request.model_copy(update={"added_edges": [ctx.request.added_edges[0].model_copy(update={"to_address": address("story-b")})]})
    await session.commit()
    with pytest.raises(SharedAppendError, match="started_wave_is_immutable"):
        await preview(session, ctx, request)


@pytest.mark.parametrize("case", ["unpaused", "service", "owner", "stale_plan", "unknown", "cycle", "empty", "remove_started", "overlap"])
async def test_invalid_changes_write_nothing(session, paused, case):  # noqa: F811
    ctx = paused
    data = ctx.request.model_dump(mode="json")
    if case == "unpaused":
        ctx.flow.execution_paused = False
    elif case == "service":
        ctx.actor = replace(ctx.actor, actor_kind=ActorKind.SERVICE)
    elif case == "owner":
        ctx.actor = replace(ctx.actor, actor_id="someone-else")
    elif case == "stale_plan":
        data["expected_plan_version"] += 1
    elif case == "unknown":
        data["added_edges"][0]["from_address"] = address("unknown")
    elif case == "cycle":
        data["added_edges"][0] = dict(from_address=address("gate", wave="wave-2"), to_address=address("story-a"))
    elif case == "empty":
        data["added_edges"] = []
    elif case == "overlap":
        data["removed_edges"] = data["added_edges"]
    else:
        await settled_history(session, ctx)
        data["removed_edges"] = [dict(from_address=address("story-a"), to_address=address("eval"))]
    await session.commit()
    with pytest.raises(SharedAppendError):
        await preview(session, ctx, WaveDependencyRequest.model_validate(data))
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1


async def test_accept_rechecks_pause_and_new_history(session, paused):  # noqa: F811
    ctx = paused
    expected = await preview(session, ctx)
    request = ctx.request.model_copy(update={"expected_snapshot": expected["snapshot"]})
    ctx.flow.execution_paused = False
    await session.commit()
    with pytest.raises(SharedAppendError, match="flow_must_be_paused"):
        await accept_wave_dependencies(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
    ctx.flow.execution_paused = True
    future = await session.get(OrchestrationNode, ctx.nodes[address("gate", wave="wave-2")])
    future.attempts = 1
    await session.commit()
    with pytest.raises(SharedAppendError, match="started_wave_is_immutable"):
        await accept_wave_dependencies(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request)


async def test_frozen_worker_completion_does_not_stale_preview(session, paused):  # noqa: F811
    ctx = paused
    saved = await settled_history(session, ctx)
    saved.node.state = "running"
    saved.execution.status = "awaiting_external"
    await session.commit()
    expected = await preview(session, ctx)
    request = ctx.request.model_copy(update={"expected_snapshot": expected["snapshot"]})
    saved.node.state = "passed"
    saved.execution.status = "concluded"
    saved.execution.revision += 1
    await session.commit()
    result = await accept_wave_dependencies(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
    assert result["accepted"]


async def test_failed_plan_write_rolls_back_graph_and_ready_state(session, paused, monkeypatch):  # noqa: F811
    ctx = paused
    future = await session.get(OrchestrationNode, ctx.nodes[address("gate", wave="wave-2")])
    future.state = "ready"
    await session.commit()
    before = len(list(await session.scalars(select(OrchestrationEdge))))
    monkeypatch.setattr(OrchestrationRepository, "record_accepted_plan", AsyncMock(side_effect=RuntimeError("failed write")))
    with pytest.raises(RuntimeError, match="failed write"):
        await accept(session, ctx)
    assert len(list(await session.scalars(select(OrchestrationEdge)))) == before
    await session.refresh(future)
    assert future.state == "ready"


async def test_lineage_is_transitive_but_rejects_general_or_tampered_plan(session, paused):  # noqa: F811
    ctx = paused
    saved = await settled_history(session, ctx)
    await accept(session, ctx)
    await session.commit()
    repo = OrchestrationRepository(session)
    second = await repo.get_accepted_plan(org_id=ctx.actor.org_id, flow_id=ctx.flow.id)
    data = ctx.request.model_dump(mode="json")
    data.update(expected_plan_version=2, expected_plan_hash=second.plan_hash, removed_edges=data["added_edges"], added_edges=[])
    await accept(session, ctx, WaveDependencyRequest.model_validate(data))
    current = await repo.get_accepted_plan(org_id=ctx.actor.org_id, flow_id=ctx.flow.id)
    assert await ancestor_plan(session, current, 1, node_id=saved.node.id) is ctx.plan
    assert (
        await receipt_plan(session, current, dict(plan_id=ctx.plan.id, plan_version=1, plan_hash=ctx.plan.plan_hash), node_id=saved.node.id)
        is ctx.plan
    )
    document = deepcopy(current.plan_document)
    document["execution_policy"]["limits"]["max_attempts_per_node"] += 1
    current.plan_document, current.plan_hash = document, digest(document)
    await session.flush()
    assert await ancestor_plan(session, current, 1, node_id=saved.node.id) is None
    # A generic new acceptance does not become compatible merely because its
    # nodes and policy look the same.
    replacement = await repo.record_accepted_plan(
        org_id=ctx.actor.org_id,
        flow_id=ctx.flow.id,
        plan_document=ctx.original,
        plan_hash=digest(ctx.original),
        accepted_by_decision_id=ctx.plan.accepted_by_decision_id,
    )
    assert await ancestor_plan(session, replacement, 1, node_id=saved.node.id) is None


async def test_merge_and_progress_keep_original_execution_current(session, paused):  # noqa: F811
    from src.orchestration.delivery_progress import node_progress
    from src.orchestration.merge_controller import code_only_delivery
    from src.orchestration.plan_lineage import preserved_execution_pairs

    ctx = paused
    saved = await settled_history(session, ctx)
    saved.node.state, saved.execution.status = "running", "blocked"
    saved.execution.block_code = "authority_unverifiable"
    await session.commit()
    await accept(session, ctx)
    identity = SimpleNamespace(accepted_plan_version=1)
    assert await code_only_delivery(session, SimpleNamespace(identity=identity), saved.node)
    pairs = await preserved_execution_pairs(session, org_id=ctx.actor.org_id, flow_ids=[ctx.flow.id])
    assert (saved.node.id, 1) in pairs
    states = await OrchestrationRepository(session).node_display_states(org_id=ctx.actor.org_id, flow_id=ctx.flow.id)
    assert states[saved.node.id] == "stalled"
    progress = node_progress(
        node=saved.node,
        binding=saved.binding,
        dispatch={},
        result={},
        execution=saved.execution,
        policy_enabled=True,
        plan_version=2,
        preserved_execution=(saved.node.id, 1) in pairs,
    )
    assert progress.blocker != "execution_stale"


async def test_exact_v0_waiver_survives_without_becoming_a_pass(session, paused):  # noqa: F811
    from src.orchestration.evaluation_waiver import WaiverRequest, accept_waiver, preview_waiver, valid_waiver
    from src.orchestration.execution_policy import ExecutionPolicy, stamp_policy

    ctx = paused
    saved = await settled_history(session, ctx)
    saved.binding.accepted_scope = json.dumps({"node": {"kind": saved.node.kind, "issue_ref": saved.node.issue_ref, "title": saved.node.title}})
    document = deepcopy(ctx.plan.plan_document)
    # Set up the V0 checkpoint with one completed, bound implementation parent.
    document["edges"] = [e for e in document["edges"] if e["from_address"] != address("story-b")]
    for edge in await session.scalars(select(OrchestrationEdge).where(OrchestrationEdge.from_node_id == ctx.nodes[address("story-b")])):
        await session.delete(edge)
    policy = {k: v for k, v in document["execution_policy"].items() if k not in {"principal_id", "policy_id", "policy_hash"}}
    policy["evaluation_acceptance"] = {address("eval"): "machine"}
    document["execution_policy"] = stamp_policy(
        ExecutionPolicy.model_validate(policy), principal_id=ctx.actor.actor_id, org_id=ctx.actor.org_id
    ).model_dump(mode="json")
    ctx.plan.plan_document, ctx.plan.plan_hash = document, digest(document)
    ctx.request = ctx.request.model_copy(update={"expected_plan_hash": ctx.plan.plan_hash})
    await session.commit()
    request = WaiverRequest(
        node_id=ctx.nodes[address("eval")],
        expected_plan_version=1,
        expected_plan_hash=ctx.plan.plan_hash,
        criterion_ids=[f"V0-{i:02d}" for i in range(1, 9)],
        reason="Owner accepts only the V0 independent evaluation gap",
    )
    previewed = await preview_waiver(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
    waiver = await accept_waiver(
        session, flow_id=ctx.flow.id, actor=ctx.actor, request=request.model_copy(update={"expected_snapshot": previewed["snapshot"]})
    )
    await session.commit()
    node = await session.get(OrchestrationNode, request.node_id)
    original = await valid_waiver(session, node)
    reason = original.reason
    await accept(session, ctx)
    await session.commit()
    preserved = await valid_waiver(session, node)
    assert preserved.id == waiver["decision_id"] and preserved.reason == reason
    assert node.state == "waived" and node.attempts == 0
    assert not json.loads(reason)["machine_pass_claimed"] and not json.loads(reason)["evaluation_executed"]
    # A changed predecessor binding still invalidates the waiver, across lineage.
    saved.binding.revision += 1
    await session.flush()
    assert await valid_waiver(session, node) is None


async def test_all_policy_supplements_and_budget_posture_are_preserved(session, paused, monkeypatch):  # noqa: F811
    from src.budget.enforcement_settings import BudgetEnforcementSetting, flow_key
    from src.orchestration import shared_budget, shared_concurrency, shared_retry, shared_window
    from src.orchestration.policy_admission import load_in_force_policy

    ctx = paused
    ctx.actor = replace(ctx.actor, actor_role="platform_admin")
    session.add(BudgetEnforcementSetting(scope_key=flow_key(ctx.actor.org_id, ctx.flow.id), enabled=False, revision=3, updated_by=ctx.actor.actor_id))
    await session.commit()
    monkeypatch.setattr("src.orchestration.shared_policy.read_flow_meter", ctx.meter)
    common = dict(expected_plan_version=1, expected_plan_hash=ctx.plan.plan_hash, reason="Owner approves the existing operating allowance")
    supplements = [
        (
            shared_budget.BudgetIncreaseRequest(**common, limits=dict(max_spend_usd=200, max_run_spend_usd=150, max_chain_spend_usd=200)),
            shared_budget.preview_budget_increase,
            shared_budget.accept_budget_increase,
        ),
        (
            shared_retry.RetryIncreaseRequest(**common, max_attempts_per_node=5),
            shared_retry.preview_retry_increase,
            shared_retry.accept_retry_increase,
        ),
        (
            shared_concurrency.ConcurrencyIncreaseRequest(**common, max_concurrent_actions=3),
            shared_concurrency.preview_concurrency_increase,
            shared_concurrency.accept_concurrency_increase,
        ),
        (
            shared_window.WindowRenewalRequest(
                **common, expires_at=datetime.fromisoformat(ctx.original["execution_policy"]["expires_at"]) + timedelta(hours=1)
            ),
            shared_window.preview_window_renewal,
            shared_window.accept_window_renewal,
        ),
    ]
    receipts = []
    for request, preview_supplement, accept_supplement in supplements:
        previewed = await preview_supplement(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
        result = await accept_supplement(
            session, flow_id=ctx.flow.id, actor=ctx.actor, request=request.model_copy(update={"expected_snapshot": previewed["snapshot"]})
        )
        receipts.append((await session.get(OrchestrationDecision, result["decision_id"])).reason)
        await session.commit()
    before = await load_in_force_policy(session, org_id=ctx.actor.org_id, flow_id=ctx.flow.id)
    await accept(session, ctx)
    after = await load_in_force_policy(session, org_id=ctx.actor.org_id, flow_id=ctx.flow.id)
    assert before.policy == after.policy  # Includes private verified receipt IDs and financial limits.
    assert before.plan_version == 1 and after.plan_version == 2
    assert not after.policy._budget_enforcement_enabled
    assert after.policy.limits.max_concurrent_actions == 3
    assert after.policy.limits.max_attempts_per_node == 5
    assert [d.reason for d in await session.scalars(select(OrchestrationDecision)) if d.reason in receipts] == receipts


async def test_wave_identity_includes_epic(session, paused):  # noqa: F811
    ctx = paused
    await settled_history(session, ctx)
    future = await session.get(OrchestrationNode, ctx.nodes[address("gate", wave="wave-2")])
    old = address("gate", wave="wave-2")
    new = address("gate", epic="epic-2", wave="wave-1")
    future.epic_ref, future.wave_ref = "epic-2", "wave-1"
    document = json.loads(json.dumps(ctx.plan.plan_document).replace(old, new))
    ctx.plan.plan_document, ctx.plan.plan_hash = document, digest(document)
    request = ctx.request.model_dump(mode="json")
    request["added_edges"][0]["to_address"] = new
    request["expected_plan_hash"] = ctx.plan.plan_hash
    await session.commit()
    result, _ = await accept(session, ctx, WaveDependencyRequest.model_validate(request))
    assert result["changed_waves"] == [("epic-2", "wave-1")]
    assert result["frozen_waves"] == [("epic-1", "wave-1")]


async def test_http_preview_accept_uses_human_snapshot_boundary(session, paused, monkeypatch):  # noqa: F811
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.orchestration import shared_amendment_routes as routes

    ctx = paused
    monkeypatch.setattr(routes, "AccessControl", lambda _: SimpleNamespace(check_permission=AsyncMock()))
    monkeypatch.setattr(
        "src.agentauth.human_control.authorize_human_session",
        AsyncMock(return_value=SimpleNamespace(tenant_id=ctx.actor.org_id, user_id=ctx.actor.actor_id)),
    )
    monkeypatch.setattr("src.orchestration.routes._resolve_actor_role", AsyncMock(return_value=ctx.actor.actor_role))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user] = lambda: SimpleNamespace(org_id=ctx.actor.org_id)
    app.dependency_overrides[routes.get_db] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = ctx.request.model_dump(mode="json")
        path = f"/flows/{ctx.flow.id}/wave-dependencies"
        previewed = await client.post(path + "/preview", json=body)
        assert previewed.status_code == 200
        assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1
        body["expected_snapshot"] = previewed.json()["snapshot"]
        accepted = await client.post(path + "/accept", json=body)
        assert accepted.status_code == 200 and accepted.json()["plan_version"] == 2
        assert ctx.flow.execution_paused


async def test_later_window_renewal_preserves_original_execution_version(session, paused):  # noqa: F811
    from src.orchestration.shared_window import WindowRenewalRequest, accept_window_renewal, preview_window_renewal

    ctx = paused
    ctx.actor = replace(ctx.actor, actor_role="platform_admin")
    saved = await settled_history(session, ctx)
    original_expiry = datetime.fromisoformat(ctx.original["execution_policy"]["expires_at"])
    saved.node.state, saved.execution.status = "running", "awaiting_external"
    # Set the cap to the original policy expiry; the approved renewal should
    # extend the same row even though it still belongs to accepted plan v1.
    saved.execution.deadline_at = original_expiry
    saved.execution.created_at = original_expiry - timedelta(minutes=10)
    await session.commit()
    result, _ = await accept(session, ctx)
    request = WindowRenewalRequest(
        expected_plan_version=2,
        expected_plan_hash=result["plan_hash"],
        expires_at=original_expiry + timedelta(hours=1),
        reason="Owner renews the existing delivery window",
    )
    previewed = await preview_window_renewal(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
    assert previewed["deadlines"][0]["execution_id"] == saved.execution.id
    await accept_window_renewal(
        session, flow_id=ctx.flow.id, actor=ctx.actor, request=request.model_copy(update={"expected_snapshot": previewed["snapshot"]})
    )
    assert saved.execution.deadline_at > original_expiry
    assert saved.execution.accepted_plan_version == 1 and saved.execution.attempts == 2
