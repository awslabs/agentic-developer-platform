"""Persisted SQL + DynamoDB + HTTP workflow dispatch and crash recovery."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.grants import TargetRelationship
from src.agentauth.routes import AgentRuntime, get_agent_runtime, router
from src.agentauth.run_credential import CREDENTIAL_KEY_ENV, verify_credential
from src.agentauth.workload import VerifiedPod
from src.orchestration.models import (
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.models.usage import UsageLog
from src.shared.models.vault import UserIdentity
from tests.agentauth.test_human_dispatch import child_dispatch as child_dispatch_fixture
from tests.agentauth.test_human_dispatch import store as store_fixture
from tests.orchestration.test_dispatch_pass import (  # noqa: F401
    _make_approval,
    _make_flow,
    _make_node,
    _make_org,
)
from tests.orchestration.test_dispatch_pass import (
    engine as engine_fixture,
)
from tests.orchestration.test_dispatch_pass import (
    session as session_fixture,
)
from tests.orchestration.test_dispatch_pass import (
    session_factory as session_factory_fixture,
)

store = store_fixture
child_dispatch = child_dispatch_fixture
engine = engine_fixture
session = session_fixture
session_factory = session_factory_fixture

if os.environ.get("ADP_GRAPH_TEST_DATABASE_URL"):

    @pytest.fixture
    async def engine():
        database = create_async_engine(os.environ["ADP_GRAPH_TEST_DATABASE_URL"])
        tables = [
            Organization.__table__,
            Department.__table__,
            Team.__table__,
            User.__table__,
            TenantMembership.__table__,
            TeamMembership.__table__,
            UserIdentity.__table__,
            UsageLog.__table__,
            OrchestrationFlow.__table__,
            OrchestrationNode.__table__,
            OrchestrationEdge.__table__,
            OrchestrationDecision.__table__,
            OrchestrationAcceptedPlan.__table__,
            OrchestrationWorkClaim.__table__,
            OrchestrationExecution.__table__,
            OrchestrationPullRequestBinding.__table__,
        ]
        async with database.begin() as connection:
            await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))
        try:
            yield database
        finally:
            async with database.begin() as connection:
                await connection.run_sync(lambda conn: Base.metadata.drop_all(conn, tables=list(reversed(tables))))
            await database.dispose()


@pytest.fixture
async def graph_context(store, child_dispatch, session, session_factory, monkeypatch, report_only_db):
    await _make_org(session, org_id="tenant", installations=[123])
    flow = await _make_flow(session, org_id="tenant")
    flow.intent_ref = "42"
    approval = await _make_approval(session, flow, actor_id="human")
    node = await _make_node(session, flow, issue_ref="43")
    await session.commit()
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session_factory)
    monkeypatch.setattr("src.agentauth.routes.verify_internal_or_irsa", AsyncMock())
    pods = {
        "root-pod-proof": VerifiedPod("pod-a", "worker-a", "adp-agents", "agent-scaledjob-sa", "10.0.1.2"),
        "child-pod-proof": VerifiedPod("pod-b", "worker-b", "adp-agents", "agent-scaledjob-sa", "10.0.1.3"),
    }
    env = {CREDENTIAL_KEY_ENV: "gateway-test-key-not-shared-with-workers", "BG_ORCH_DISPATCH_REPO": "org/repo"}
    runtime = AgentRuntime(store=store, workloads=SimpleNamespace(verify=lambda token: pods[token]), env=env, dispatcher=child_dispatch.service)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    from src.shared.database import get_db

    app.dependency_overrides[get_db] = report_only_db
    # Use the real header name; the projected proof is distinct from transport.
    from src.agentauth.workload import WORKLOAD_HEADER

    headers = {"X-Caller-Identity": "shared-role", WORKLOAD_HEADER: "root-pod-proof"}
    original = store._read("TENANT#tenant", f"EXEC#{child_dispatch.invocation}")
    bootstrap = {"invocation_id": child_dispatch.invocation, "envelope_digest": original["envelope_digest"]["S"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        yield SimpleNamespace(
            client=client,
            runtime=runtime,
            flow=flow,
            node=node,
            approval=approval,
            headers=headers,
            bootstrap=bootstrap,
            body={"persona": "developer", "target": {"repo": "org/repo", "issue": 43}, "request_id": "graph-request", "reason": "approved work"},
            child=child_dispatch,
            store=store,
            session_factory=session_factory,
        )


async def enroll(ctx):
    response = await ctx.client.post("/internal/v1/agent/bootstrap", json=ctx.bootstrap, headers=ctx.headers)
    assert response.status_code == 200, response.text
    ctx.headers["X-Adp-Run-Credential"] = response.json()["credential"]
    return response.json()


async def send(ctx, body=None):
    return await ctx.client.post("/internal/v1/agent/dispatch", json=body or ctx.body, headers=ctx.headers)


def messages(ctx):
    return ctx.child.sqs.receive_message(QueueUrl=ctx.child.queue, MaxNumberOfMessages=10).get("Messages", [])


async def node_state(ctx):
    async with ctx.session_factory() as session:
        node = await session.get(OrchestrationNode, ctx.node.id)
        return node.state, node.attempts


async def test_bootstrap_assigns_only_approved_intent_and_preserves_ceilings(graph_context):
    ctx = graph_context
    before = ctx.store.authority.load_grant(principal=f"{ctx.child.invocation}#1", tenant_id="tenant")
    result = await enroll(ctx)
    caller = verify_credential(result["credential"], env=ctx.runtime.env)
    after = ctx.store.live_grant(invocation_id=caller.invocation_id, tenant_id="tenant", attempt=1, now=datetime.now(UTC))
    assert caller.flow_id == ctx.flow.id == after.flow_id
    assert after.authority.reference_id == ctx.approval.id
    assert TargetRelationship.FLOW_NODE in after.target_relationships
    assert after.allowed_actions == before.allowed_actions
    assert after.delegable_actions == before.delegable_actions
    assert after.max_dispatch_concurrency == before.max_dispatch_concurrency
    assert after.revocation_epoch == before.revocation_epoch + 1
    assert after.expires_at <= before.expires_at
    assert (await enroll(ctx))["attempt"] == 1


async def test_coordinator_dispatch_commit_retry_child_bootstrap_and_monitor(graph_context):
    ctx = graph_context
    await enroll(ctx)
    first = await send(ctx)
    assert first.status_code == 202, first.text
    assert (await send(ctx)).json() == first.json()
    assert await node_state(ctx) == ("running", 1)
    queued = messages(ctx)
    assert len(queued) == 1
    envelope = json.loads(queued[0]["Body"])
    assert envelope["orchestration"]["node_id"] == ctx.node.id
    from src.agentauth.workload import WORKLOAD_HEADER

    boot = await ctx.client.post(
        "/internal/v1/agent/bootstrap",
        json={"invocation_id": envelope["message_id"], "envelope_digest": envelope_digest(envelope)},
        headers={"X-Caller-Identity": "shared-role", WORKLOAD_HEADER: "child-pod-proof"},
    )
    assert boot.status_code == 200, boot.text
    status = await ctx.client.get("/internal/v1/agent/status", params={"run": envelope["message_id"]}, headers=ctx.headers)
    assert status.status_code == 200, status.text
    async with ctx.session_factory() as session:
        receipts = list(
            (
                await session.execute(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.kind == DecisionKind.AGENT_DISPATCHED.value,
                    )
                )
            ).scalars()
        )
        assert len(receipts) == 1
        assert receipts[0].actor_kind == "service"
        assert receipts[0].actor_id == f"agent:{ctx.child.invocation}#1"
        assert ctx.body["reason"] not in receipts[0].reason


@pytest.mark.parametrize("change", ["wrong_issue", "wrong_repo", "reviewer_before_developer", "gate", "halted", "ambiguous_issue"])
async def test_ineligible_graph_work_is_refused_without_reservation(graph_context, change):
    ctx = graph_context
    await enroll(ctx)
    body = json.loads(json.dumps(ctx.body))
    if change == "wrong_issue":
        body["target"]["issue"] = 999
    elif change == "wrong_repo":
        body["target"]["repo"] = "other/repo"
    elif change == "reviewer_before_developer":
        body["persona"] = "reviewer"
    else:
        async with ctx.session_factory() as session:
            if change == "ambiguous_issue":
                await _make_node(session, ctx.flow, issue_ref="43", node_ref="duplicate")
            else:
                await session.execute(
                    update(OrchestrationNode)
                    .where(OrchestrationNode.id == ctx.node.id)
                    .values(**({"kind": "gate"} if change == "gate" else {"state": "halted"}))
                )
            await session.commit()
    assert (await send(ctx, body)).status_code == 404
    assert messages(ctx) == []
    assert ctx.store.authority.active_dispatch_count(grant_id=f"grant:{ctx.child.invocation}:1", tenant_id="tenant") == 0


async def test_reviewer_uses_running_story_without_another_node_transition(graph_context):
    ctx = graph_context
    await enroll(ctx)
    assert (await send(ctx)).status_code == 202
    response = await send(ctx, {**ctx.body, "persona": "reviewer", "request_id": "review-story"})
    assert response.status_code == 202, response.text
    assert await node_state(ctx) == ("running", 1)
    assert len(messages(ctx)) == 2


async def test_rollback_after_reservation_recovers_same_child_without_early_publication(graph_context, monkeypatch):
    ctx = graph_context
    await enroll(ctx)
    from src.agentauth import graph_dispatch

    original = graph_dispatch.dispatch_node
    monkeypatch.setattr(graph_dispatch, "dispatch_node", AsyncMock(side_effect=RuntimeError("SQL unavailable")))
    with pytest.raises(RuntimeError, match="SQL unavailable"):
        await send(ctx)
    assert messages(ctx) == []
    assert await node_state(ctx) == ("ready", 0)
    key = envelope_digest({"principal": f"{ctx.child.invocation}#1", "request_id": "graph-request"})
    command = ctx.store._read("TENANT#tenant", f"DISPATCH#{key}")
    assert command is not None
    monkeypatch.setattr(graph_dispatch, "dispatch_node", original)
    response = await send(ctx)
    assert response.status_code == 202, response.text
    assert response.json()["invocation_id"] == command["invocation_id"]["S"]
    assert await node_state(ctx) == ("running", 1)
    assert len(messages(ctx)) == 1


async def test_lost_sql_commit_response_recovers_receipt(graph_context, monkeypatch):
    ctx = graph_context
    await enroll(ctx)
    original = AsyncSession.commit
    lost = [True]

    async def commit(session):
        await original(session)
        if lost.pop() if lost else False:
            raise RuntimeError("commit reply lost")

    monkeypatch.setattr(AsyncSession, "commit", commit)
    with pytest.raises(RuntimeError, match="commit reply lost"):
        await send(ctx)
    assert await node_state(ctx) == ("running", 1)
    assert messages(ctx) == []
    assert (await send(ctx)).status_code == 202
    assert len(messages(ctx)) == 1


async def test_lost_queue_response_is_one_child_and_one_attempt(graph_context, monkeypatch):
    ctx = graph_context
    await enroll(ctx)
    original = ctx.child.sqs.send_message
    attempts = []

    def send_message(**kwargs):
        attempts.append(kwargs["MessageDeduplicationId"])
        result = original(**kwargs)
        if len(attempts) == 1:
            raise EndpointConnectionError(endpoint_url="https://queue.test")
        return result

    monkeypatch.setattr(ctx.child.sqs, "send_message", send_message)
    assert (await send(ctx)).status_code == 503
    assert (await send(ctx)).status_code == 202
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert len(messages(ctx)) == 1
    assert await node_state(ctx) == ("running", 1)


@pytest.mark.parametrize("source", ["launch", "flow", "grant"])
async def test_revocation_stops_coordinator_refresh_and_dispatch(graph_context, source):
    ctx = graph_context
    await enroll(ctx)
    raw = ctx.store._read("TENANT#tenant", f"GRANT#{ctx.child.invocation}#1")
    if source == "flow":
        async with ctx.session_factory() as session:
            await session.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).values(state="halted"))
            await session.commit()
    else:
        key = f"AUTHORITY#{raw['launch_authority_reference_id']['S']}" if source == "launch" else f"GRANT#{ctx.child.invocation}#1"
        ctx.store.client.update_item(
            TableName=ctx.store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": key}},
            UpdateExpression="SET #f = :v",
            ExpressionAttributeNames={"#f": "status" if source == "launch" else "revoked"},
            ExpressionAttributeValues={":v": {"S": "revoked"} if source == "launch" else {"BOOL": True}},
        )
    assert (await send(ctx)).status_code == 404
    boot = await ctx.client.post("/internal/v1/agent/bootstrap", json=ctx.bootstrap, headers=ctx.headers)
    assert boot.status_code == 404
    assert messages(ctx) == []


async def test_developer_can_request_review_only_for_its_assigned_story(graph_context):
    ctx = graph_context
    await enroll(ctx)
    assert (await send(ctx)).status_code == 202
    envelope = json.loads(messages(ctx)[0]["Body"])
    from src.agentauth.workload import WORKLOAD_HEADER

    child_headers = {"X-Caller-Identity": "shared-role", WORKLOAD_HEADER: "child-pod-proof"}
    response = await ctx.client.post(
        "/internal/v1/agent/bootstrap",
        json={"invocation_id": envelope["message_id"], "envelope_digest": envelope_digest(envelope)},
        headers=child_headers,
    )
    assert response.status_code == 200
    child_headers["X-Adp-Run-Credential"] = response.json()["credential"]
    review = {**ctx.body, "persona": "reviewer", "request_id": "developer-review"}
    response = await ctx.client.post("/internal/v1/agent/dispatch", json=review, headers=child_headers)
    assert response.status_code == 202, response.text
    assert await node_state(ctx) == ("running", 1)
    denied = await ctx.client.post("/internal/v1/agent/dispatch", json={**review, "target": {"repo": "org/repo", "issue": 42}}, headers=child_headers)
    assert denied.status_code == 404


async def test_same_request_with_changed_intent_or_attempt_cannot_dispatch_again(graph_context):
    ctx = graph_context
    await enroll(ctx)
    assert (await send(ctx)).status_code == 202
    assert (await send(ctx, {**ctx.body, "reason": "changed instructions"})).status_code == 409
    async with ctx.session_factory() as db:
        await db.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.node.id).values(attempts=2))
        await db.commit()
    assert (await send(ctx)).status_code == 409
    assert len(messages(ctx)) == 1


async def test_queued_child_cannot_bootstrap_after_flow_cancellation(graph_context):
    ctx = graph_context
    await enroll(ctx)
    assert (await send(ctx)).status_code == 202
    envelope = json.loads(messages(ctx)[0]["Body"])
    async with ctx.session_factory() as db:
        await db.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).values(state="halted"))
        await db.commit()
    from src.agentauth.workload import WORKLOAD_HEADER

    response = await ctx.client.post(
        "/internal/v1/agent/bootstrap",
        json={"invocation_id": envelope["message_id"], "envelope_digest": envelope_digest(envelope)},
        headers={"X-Caller-Identity": "shared-role", WORKLOAD_HEADER: "child-pod-proof"},
    )
    assert response.status_code == 404
    assert "credential" not in response.text


@pytest.mark.skipif(not os.environ.get("ADP_GRAPH_TEST_DATABASE_URL"), reason="requires isolated PostgreSQL row locks")
async def test_overlapping_requests_commit_only_one_node_attempt(graph_context):
    ctx = graph_context
    await enroll(ctx)
    responses = await asyncio.gather(send(ctx), send(ctx))
    assert [response.status_code for response in responses] == [202, 202]
    assert responses[0].json() == responses[1].json()
    assert await node_state(ctx) == ("running", 1)
    assert len(messages(ctx)) == 1


@pytest.mark.skipif(not os.environ.get("ADP_GRAPH_TEST_DATABASE_URL"), reason="requires isolated PostgreSQL row locks")
async def test_distinct_overlapping_requests_cannot_start_two_developers(graph_context):
    ctx = graph_context
    await enroll(ctx)
    responses = await asyncio.gather(send(ctx), send(ctx, {**ctx.body, "request_id": "competing-request"}))
    assert sorted(response.status_code for response in responses) == [202, 404]
    assert await node_state(ctx) == ("running", 1)
    assert len(messages(ctx)) == 1


@pytest.mark.parametrize("reason", ["different_intent", "unapproved"])
async def test_bootstrap_does_not_assign_unapproved_or_unrelated_flow(graph_context, reason):
    ctx = graph_context
    async with ctx.session_factory() as db:
        await db.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).values(intent_ref="999"))
        if reason == "unapproved":
            unapproved = await _make_flow(db, org_id="tenant", slug="unapproved")
            unapproved.intent_ref = "42"
        await db.commit()
    await enroll(ctx)
    grant = ctx.store.authority.load_grant(principal=f"{ctx.child.invocation}#1", tenant_id="tenant")
    assert grant.authority.kind == "github_event"
    assert TargetRelationship.FLOW_NODE not in grant.target_relationships
    assert (await send(ctx)).status_code == 404


async def test_flow_issue_number_in_the_configured_repository_does_not_block_another_repository(graph_context):
    """Numeric graph references belong only to the engine's configured repo."""
    ctx = graph_context
    ctx.runtime.env["BG_ORCH_DISPATCH_REPO"] = "another/repo"
    await enroll(ctx)
    grant = ctx.store.authority.load_grant(principal=f"{ctx.child.invocation}#1", tenant_id="tenant")
    assert grant.authority.kind == "github_event"
    assert TargetRelationship.FLOW_NODE not in grant.target_relationships

    response = await send(ctx)
    assert response.status_code == 202, response.text
    envelope = json.loads(messages(ctx)[0]["Body"])
    assert envelope["source_ref"]["repo"] == "org/repo"
    assert "orchestration" not in envelope


async def test_coordinator_assignment_lost_reply_recovers_committed_grant(graph_context, monkeypatch):
    ctx = graph_context
    original = ctx.store.client.transact_write_items

    def lost_reply(**kwargs):
        result = original(**kwargs)
        if any("coordinator_flow_id" in action.get("Update", {}).get("UpdateExpression", "") for action in kwargs["TransactItems"]):
            raise EndpointConnectionError(endpoint_url="https://store.test")
        return result

    monkeypatch.setattr(ctx.store.client, "transact_write_items", lost_reply)
    await enroll(ctx)
    assert (await send(ctx)).status_code == 202


async def test_racing_grant_reduction_is_not_overwritten_by_coordinator_assignment(graph_context, monkeypatch):
    ctx = graph_context
    original = ctx.store.client.transact_write_items

    def reduce_grant(**kwargs):
        if any("coordinator_flow_id" in action.get("Update", {}).get("UpdateExpression", "") for action in kwargs["TransactItems"]):
            ctx.store.client.update_item(
                TableName=ctx.store.table,
                Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"GRANT#{ctx.child.invocation}#1"}},
                UpdateExpression="SET max_dispatch_concurrency = :zero, revocation_epoch = :epoch",
                ExpressionAttributeValues={":zero": {"N": "0"}, ":epoch": {"N": "7"}},
            )
        return original(**kwargs)

    monkeypatch.setattr(ctx.store.client, "transact_write_items", reduce_grant)
    response = await ctx.client.post("/internal/v1/agent/bootstrap", json=ctx.bootstrap, headers=ctx.headers)
    assert response.status_code == 503
    grant = ctx.store.authority.load_grant(principal=f"{ctx.child.invocation}#1", tenant_id="tenant")
    assert grant.max_dispatch_concurrency == 0 and grant.revocation_epoch == 7
    assert grant.authority.kind == "github_event"


@pytest.mark.skipif(not os.environ.get("ADP_GRAPH_TEST_DATABASE_URL"), reason="requires isolated PostgreSQL row locks")
async def test_cancellation_holding_flow_lock_prevents_dispatch(graph_context):
    ctx = graph_context
    await enroll(ctx)
    async with ctx.session_factory() as db:
        await db.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).values(state="halted"))
        dispatch = asyncio.create_task(send(ctx))
        await asyncio.sleep(0.05)
        assert not dispatch.done()
        await db.commit()
    assert (await dispatch).status_code == 404
    assert await node_state(ctx) == ("ready", 0)
    assert messages(ctx) == []


async def test_workflow_refusal_audits_real_caller_without_instruction_body(graph_context, caplog):
    ctx = graph_context
    await enroll(ctx)
    caplog.set_level("INFO", logger="bedrockgateway.agentauth.routes")
    response = await send(ctx, {**ctx.body, "target": {"repo": "org/repo", "issue": 999}, "reason": "private-instruction-canary"})
    assert response.status_code == 404
    record = next(record for record in caplog.records if record.getMessage() == "Agent request outcome")
    assert record.principal == f"{ctx.child.invocation}#1"
    assert record.authority_reference_id == ctx.approval.id
    assert record.target == "org/repo#999" and record.action == "dispatch" and record.outcome == "refused"
    assert "private-instruction-canary" not in caplog.text


async def test_scope_changed_during_live_grant_read_is_refused(graph_context, monkeypatch):
    ctx = graph_context
    await enroll(ctx)
    original = ctx.store.authority.load_grant

    def change_scope(**kwargs):
        grant = original(**kwargs)
        ctx.store.client.update_item(
            TableName=ctx.store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"GRANT#{ctx.child.invocation}#1"}},
            UpdateExpression="SET revocation_epoch = :epoch",
            ExpressionAttributeValues={":epoch": {"N": "7"}},
        )
        return grant

    monkeypatch.setattr(ctx.store.authority, "load_grant", change_scope)
    assert (await send(ctx)).status_code == 404
    assert messages(ctx) == []


async def test_uncommitted_child_cannot_borrow_another_dispatch_node_state(graph_context, monkeypatch):
    ctx = graph_context
    await enroll(ctx)
    from src.agentauth import graph_dispatch
    from src.agentauth.workload import WORKLOAD_HEADER
    from src.orchestration.dispatch import dispatch_node
    from src.orchestration.genesis import resolve_engine_genesis

    monkeypatch.setattr(graph_dispatch, "dispatch_node", AsyncMock(side_effect=RuntimeError("SQL rollback")))
    with pytest.raises(RuntimeError, match="SQL rollback"):
        await send(ctx)
    key = envelope_digest({"principal": f"{ctx.child.invocation}#1", "request_id": "graph-request"})
    command = ctx.store._read("TENANT#tenant", f"DISPATCH#{key}")
    envelope = json.loads(command["envelope_json"]["S"])
    # The scheduled engine could now dispatch this still-ready node. Its running
    # state is not proof that the abandoned coordinator request was committed.
    async with ctx.session_factory() as db:
        genesis = await resolve_engine_genesis(db, org_id="tenant", decision_id=ctx.approval.id)
        assert (await dispatch_node(db, ctx.node.id, genesis)).dispatched
        await db.commit()
    response = await ctx.client.post(
        "/internal/v1/agent/bootstrap",
        json={"invocation_id": envelope["message_id"], "envelope_digest": envelope_digest(envelope)},
        headers={"X-Caller-Identity": "shared-role", WORKLOAD_HEADER: "child-pod-proof"},
    )
    assert response.status_code == 404
    assert messages(ctx) == []
