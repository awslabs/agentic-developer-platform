"""Materialized AI-DLC handoffs through real HTTP, SQL and protected DDB."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import select, update

from src.agentauth.grants import TargetRelationship
from src.agentauth.waves import wave_key
from src.agentauth.workload import WORKLOAD_HEADER, VerifiedPod
from src.orchestration.models import DecisionKind, OrchestrationDecision, OrchestrationNode
from tests.agentauth.test_graph_dispatch import (  # noqa: F401
    child_dispatch,  # noqa: F401
    engine,
    enroll,
    messages,
    session,
    session_factory,
    store,
)
from tests.agentauth.test_graph_dispatch import (
    graph_context as graph_context_fixture,
)
from tests.orchestration.test_dispatch_pass import _make_approval, _make_node

graph_context = graph_context_fixture


@pytest.fixture
async def wave_context(graph_context, monkeypatch):
    ctx = graph_context
    async with ctx.session_factory() as sql:
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.node.id).values(epic_ref="epic-4191", wave_ref="wave-1"))
        evaluation = await _make_node(sql, ctx.flow, node_ref="eval", kind="eval", issue_ref=None, state="pending")
        evaluation.epic_ref, evaluation.wave_ref = "epic-4191", "wave-1"
        await sql.commit()
    ctx.evaluation_id = evaluation.id
    ctx.binding = {"repo": "org/repo", "epic_ref": "epic-4191", "wave_ref": "wave-1", "orchestrator_issue": 44, "evaluation_issue": 45}
    ctx.verify_issues = AsyncMock()
    monkeypatch.setattr("src.agentauth.waves.verify_materialized_issues", ctx.verify_issues)
    await enroll(ctx)
    return ctx


async def bind(ctx, **changes):
    return await ctx.client.post("/internal/v1/agent/waves", json={**ctx.binding, **changes}, headers=ctx.headers)


async def dispatch(ctx, persona="operations", issue=44, request_id="wave-start", headers=None):
    return await ctx.client.post(
        "/internal/v1/agent/dispatch",
        headers=headers or ctx.headers,
        json={
            "persona": persona,
            "target": {"repo": "org/repo", "issue": issue},
            "request_id": request_id,
        },
    )


async def bootstrap_child(ctx, invocation):
    row = ctx.store._read("TENANT#tenant", f"EXEC#{invocation}")
    original = ctx.runtime.workloads.verify
    proof = "proof-" + invocation
    ctx.runtime.workloads.verify = lambda token: (
        VerifiedPod(proof, "worker", "adp-agents", "agent-scaledjob-sa", "10.0.1.3") if token == proof else original(token)
    )
    headers = {"X-Caller-Identity": "shared-role", WORKLOAD_HEADER: proof}
    response = await ctx.client.post(
        "/internal/v1/agent/bootstrap",
        headers=headers,
        json={
            "invocation_id": invocation,
            "envelope_digest": row["envelope_digest"]["S"],
        },
    )
    if response.status_code == 200:
        headers["X-Adp-Run-Credential"] = response.json()["credential"]
    return response, headers


async def start_wave(ctx):
    assert (await bind(ctx)).status_code == 200
    launch = await dispatch(ctx)
    assert launch.status_code == 202, launch.text
    boot, headers = await bootstrap_child(ctx, launch.json()["invocation_id"])
    assert boot.status_code == 200, boot.text
    return launch, headers


async def test_emitter_coordinator_developer_reviewer_and_evaluation(wave_context):
    ctx = wave_context
    launch, headers = await start_wave(ctx)
    assert (await bind(ctx)).status_code == 200
    assert (await dispatch(ctx)).json() == launch.json()
    assert (await dispatch(ctx, request_id="duplicate-wave")).status_code == 409
    row = ctx.store._read("TENANT#tenant", f"GRANT#{launch.json()['invocation_id']}#1")
    assert row["target_relationships"]["SS"] == ["descendant", "self"]
    assert TargetRelationship.FLOW_NODE.value not in row["target_relationships"]["SS"]
    # A coordinator launch does not execute its EVAL anchor.
    async with ctx.session_factory() as sql:
        evaluation = await sql.get(OrchestrationNode, ctx.evaluation_id)
        assert (evaluation.state, evaluation.attempts) == ("pending", 0)
    assert (await dispatch(ctx, issue=45, request_id="early-eval", headers=headers)).status_code == 404
    developer = await dispatch(ctx, persona="developer", issue=43, request_id="story", headers=headers)
    assert developer.status_code == 202, developer.text
    boot, developer_headers = await bootstrap_child(ctx, developer.json()["invocation_id"])
    assert boot.status_code == 200, boot.text
    review = await dispatch(ctx, persona="reviewer", issue=43, request_id="review", headers=developer_headers)
    assert review.status_code == 202, review.text
    assert (await bootstrap_child(ctx, review.json()["invocation_id"]))[0].status_code == 200
    status = await ctx.client.get("/internal/v1/agent/status", params={"run": review.json()["invocation_id"]}, headers=headers)
    assert status.status_code == 200
    # The existing engine releases eval after dependencies pass. This test is
    # about dispatch consuming that state, not bypassing the engine's gates.
    async with ctx.session_factory() as sql:
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.node.id).values(state="passed"))
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.evaluation_id).values(state="ready"))
        await sql.commit()
    evaluation = await dispatch(ctx, issue=45, request_id="eval", headers=headers)
    assert evaluation.status_code == 202, evaluation.text
    assert (await bootstrap_child(ctx, evaluation.json()["invocation_id"]))[0].status_code == 200
    async with ctx.session_factory() as sql:
        node = await sql.get(OrchestrationNode, ctx.evaluation_id)
        assert (node.state, node.attempts) == ("running", 1)
        receipts = list(
            (
                await sql.execute(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.WAVE_COORDINATOR_DISPATCHED.value))
            ).scalars()
        )
        assert len(receipts) == 1 and receipts[0].actor_kind == "service"


async def test_wave_cannot_dispatch_other_wave_or_bind_more_work(wave_context):
    ctx = wave_context
    _, headers = await start_wave(ctx)
    async with ctx.session_factory() as sql:
        other = await _make_node(sql, ctx.flow, issue_ref="90", node_ref="other")
        other.wave_ref = "wave-2"
        await sql.commit()
    assert (await dispatch(ctx, persona="developer", issue=90, request_id="cross-wave", headers=headers)).status_code == 404
    assert (await ctx.client.post("/internal/v1/agent/waves", json=ctx.binding, headers=headers)).status_code == 404
    assert (await dispatch(ctx, issue=44, request_id="self-launch", headers=headers)).status_code == 404


@pytest.mark.parametrize("change", ["new_node", "changed_issue", "amended_plan", "cancelled", "missing_receipt", "bad_alias"])
async def test_changed_or_uncommitted_wave_cannot_bootstrap(wave_context, change):
    ctx = wave_context
    assert (await bind(ctx)).status_code == 200
    launch = await dispatch(ctx)
    assert launch.status_code == 202, launch.text
    if change == "missing_receipt":
        execution = ctx.store._read("TENANT#tenant", f"EXEC#{launch.json()['invocation_id']}")
        execution["orchestration_dispatch_receipt"] = {"S": "missing"}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=execution)
    elif change == "bad_alias":
        ctx.store.client.put_item(
            TableName=ctx.store.table, Item={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "WAVEISSUE#org/repo#44"}, "digest": {"S": "bad"}}
        )
    else:
        async with ctx.session_factory() as sql:
            if change == "new_node":
                node = await _make_node(sql, ctx.flow, node_ref="added", issue_ref="55")
                node.epic_ref, node.wave_ref = "epic-4191", "wave-1"
            elif change == "changed_issue":
                await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.node.id).values(issue_ref="55"))
            elif change == "amended_plan":
                await _make_approval(sql, ctx.flow, kind=DecisionKind.PLAN_AMENDED.value)
            else:
                from src.orchestration.models import OrchestrationFlow

                await sql.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).values(state="halted"))
            await sql.commit()
    assert (await bootstrap_child(ctx, launch.json()["invocation_id"]))[0].status_code == 404


async def test_binding_recovers_lost_ddb_reply_and_refuses_conflicts(wave_context, monkeypatch):
    ctx = wave_context
    original = ctx.store.client.transact_write_items

    def lost_reply(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://dynamodb.test")

    monkeypatch.setattr(ctx.store.client, "transact_write_items", lost_reply)
    assert (await bind(ctx)).status_code == 200
    assert (await bind(ctx)).status_code == 200
    assert (await bind(ctx, orchestrator_issue=99)).status_code == 409
    key = wave_key(ctx.flow.id, "epic-4191", "wave-1")
    mapping = json.loads(ctx.store._read("TENANT#tenant", key)["mapping_json"]["S"])
    assert mapping["orchestrator_issue"] == 44


async def test_native_parent_is_verified_and_failure_has_no_binding(wave_context):
    from src.agentauth.bootstrap import BootstrapRefusedError

    ctx = wave_context
    ctx.verify_issues.side_effect = BootstrapRefusedError("wrong epic")
    assert (await bind(ctx)).status_code == 404
    assert ctx.store._read("TENANT#tenant", wave_key(ctx.flow.id, "epic-4191", "wave-1")) is None
    assert messages(ctx) == []


@pytest.mark.parametrize("passed_gate", [False, True])
async def test_next_wave_requires_passed_evaluation_and_human_gate(wave_context, passed_gate):
    from src.orchestration.models import OrchestrationEdge

    ctx = wave_context
    launch, headers = await start_wave(ctx)
    async with ctx.session_factory() as sql:
        next_story = await _make_node(sql, ctx.flow, node_ref="next-story", issue_ref="90")
        next_eval = await _make_node(sql, ctx.flow, node_ref="next-eval", issue_ref=None, kind="eval", state="pending")
        gate = await _make_node(sql, ctx.flow, node_ref="boundary-gate", issue_ref=None, kind="gate", state="awaiting_gate")
        for node in (next_story, next_eval, gate):
            node.epic_ref, node.wave_ref = "epic-4191", "wave-2"
        sql.add_all(
            [
                OrchestrationEdge(org_id="tenant", flow_id=ctx.flow.id, from_node_id=ctx.evaluation_id, to_node_id=gate.id),
                OrchestrationEdge(org_id="tenant", flow_id=ctx.flow.id, from_node_id=gate.id, to_node_id=next_story.id),
            ]
        )
        await sql.commit()
    assert (await bind(ctx, wave_ref="wave-2", orchestrator_issue=91, evaluation_issue=92)).status_code == 200
    assert (await dispatch(ctx, issue=91, request_id="next-wave", headers=headers)).status_code == 404
    async with ctx.session_factory() as sql:
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.evaluation_id).values(state="passed"))
        if passed_gate:
            await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == gate.id).values(state="passed"))
        await sql.commit()
    response = await dispatch(ctx, issue=91, request_id="next-wave", headers=headers)
    assert response.status_code == (202 if passed_gate else 404), response.text
    if passed_gate:
        assert (await bootstrap_child(ctx, response.json()["invocation_id"]))[0].status_code == 200


@pytest.mark.skipif(not os.environ.get("ADP_GRAPH_TEST_DATABASE_URL"), reason="requires PostgreSQL row locks")
async def test_concurrent_materialization_and_wave_launch_are_unique(wave_context):
    ctx = wave_context
    bound = await asyncio.gather(bind(ctx), bind(ctx))
    assert [response.status_code for response in bound] == [200, 200]
    launched = await asyncio.gather(dispatch(ctx), dispatch(ctx, request_id="competing-wave"))
    assert sorted(response.status_code for response in launched) == [202, 409]
    assert len(messages(ctx)) == 1


async def test_revocation_after_mapping_commit_never_creates_sql_receipt(wave_context, monkeypatch):
    ctx = wave_context
    original = ctx.store.client.transact_write_items

    def revoke_after_mapping(**kwargs):
        result = original(**kwargs)
        if any(action.get("Put", {}).get("Item", {}).get("sk", {}).get("S", "").startswith("WAVE#") for action in kwargs["TransactItems"]):
            grant = ctx.store._read("TENANT#tenant", f"GRANT#{ctx.child.invocation}#1")
            grant["revoked"] = {"BOOL": True}
            ctx.store.client.put_item(TableName=ctx.store.table, Item=grant)
            raise EndpointConnectionError(endpoint_url="https://dynamodb.test")
        return result

    monkeypatch.setattr(ctx.store.client, "transact_write_items", revoke_after_mapping)
    assert (await bind(ctx)).status_code == 404
    async with ctx.session_factory() as sql:
        assert (
            await sql.execute(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == DecisionKind.WAVE_MATERIALIZED.value))
        ).first() is None
