"""Real SQL dispatch -> protected reservation -> reviewer-envelope contract."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.execution_state import ExecutionIdentity
from src.orchestration.execution_store import create_execution
from src.orchestration.models import OrchestrationAction, OrchestrationExecution, OrchestrationPullRequestBinding, OrchestrationWorkClaim
from src.orchestration.pr_bindings import PullRequestIdentity
from src.orchestration.pr_identity import PrIdentityError
from tests.agentauth import test_graph_dispatch as graph_tests
from tests.agentauth.test_graph_dispatch import (  # noqa: F401
    child_dispatch,
    enroll,
    messages,
    send,
    session,
    session_factory,
    store,
)
from tests.agentauth.test_graph_dispatch import graph_context as graph_context_fixture
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.orchestration.test_policy_admission import healthy_policy_reservations, policy_budget_initializers  # noqa: F401

HEAD = "a" * 40
MOVED_HEAD = "b" * 40
graph_context = graph_context_fixture


@pytest.fixture
async def engine(pg_url):  # noqa: F811
    database = create_async_engine(to_async_url(pg_url))
    models = (
        graph_tests.Organization,
        graph_tests.Department,
        graph_tests.Team,
        graph_tests.User,
        graph_tests.TenantMembership,
        graph_tests.TeamMembership,
        graph_tests.UserIdentity,
        graph_tests.UsageLog,
        graph_tests.OrchestrationFlow,
        graph_tests.OrchestrationNode,
        graph_tests.OrchestrationEdge,
        graph_tests.OrchestrationDecision,
        graph_tests.OrchestrationAcceptedPlan,
        OrchestrationWorkClaim,
        OrchestrationExecution,
        OrchestrationAction,
        OrchestrationPullRequestBinding,
    )
    async with database.begin() as connection:
        await connection.run_sync(lambda connection: graph_tests.Base.metadata.create_all(connection, tables=[m.__table__ for m in models]))
    yield database
    await database.dispose()


async def seed_review_context(ctx, monkeypatch):
    """A policy-bound developer has delivered a registered PR for its live cycle."""
    author = attempt_run_id(ctx.node.id, 1)
    identity = ExecutionIdentity("tenant", ctx.node.id, 1, 1, "review-claim", 1)
    async with ctx.session_factory() as db:
        db.add(
            OrchestrationWorkClaim(
                id=identity.claim_id,
                org_id="tenant",
                provider_repository_id=1234,
                issue_number=43,
                owner_kind="engine_flow",
                owner_ref=ctx.flow.id,
                state="held",
                generation=1,
            )
        )
        db.add(
            OrchestrationPullRequestBinding(
                org_id="tenant",
                flow_id=ctx.flow.id,
                node_id=ctx.node.id,
                attempt=1,
                run_id=author,
                provider_repository_id=1234,
                provider_pr_node_id="PR_bound",
                repo="org/repo",
                pr_number=77,
                installation_id=123,
                head_sha=HEAD,
                revision=1,
                role="implementation",
                state="active",
                registered_by=author,
                registered_by_kind="service",
            )
        )
        await db.flush()
        outcome = await create_execution(db, identity=identity, flow_id=ctx.flow.id)
        assert outcome.record is not None
        execution_id = outcome.record.id
        await db.commit()
    provider = AsyncMock(return_value=PullRequestIdentity(1234, "PR_bound", "org/repo", 77, HEAD))
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_pr_identity", provider)
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_head_check_runs", AsyncMock(return_value=frozenset()))
    return SimpleNamespace(
        ctx=ctx,
        identity=identity,
        author=author,
        execution_id=execution_id,
        provider=provider,
        body={**ctx.body, "persona": "reviewer", "request_id": "review-evidence"},
    )


@pytest.fixture
async def review_context(graph_context, monkeypatch):
    from tests.agentauth.test_graph_policy_dispatch import accept

    ctx = graph_context
    await accept(ctx)
    await enroll(ctx)
    assert (await send(ctx)).status_code == 202
    assert len(messages(ctx)) == 1
    return await seed_review_context(ctx, monkeypatch)


async def test_review_envelope_carries_protected_fences_and_replays_once(review_context):
    r = review_context
    first = await send(r.ctx, r.body)
    assert first.status_code == 202, first.text
    assert (await send(r.ctx, r.body)).json() == first.json()
    queued = messages(r.ctx)
    assert len(queued) == 1
    envelope = json.loads(queued[0]["Body"])
    assert envelope["review_expect"] == {
        "org_id": "tenant",
        "flow_id": r.ctx.flow.id,
        "node_id": r.ctx.node.id,
        "cycle": 1,
        "accepted_plan_version": 1,
        "claim_id": "review-claim",
        "claim_generation": 1,
        "author_run_id": r.author,
        "execution_id": r.execution_id,
        "expected_head_sha": HEAD,
        "repo": "org/repo",
        "pr_number": 77,
        "provider_repository_id": 1234,
        "provider_pr_node_id": "PR_bound",
    }
    assert envelope["message_id"] != r.author
    assert "handoff_required" not in envelope
    execution = r.ctx.store._read("TENANT#tenant", f"EXEC#{envelope['message_id']}")
    assert execution["envelope_digest"]["S"] == graph_tests.envelope_digest(envelope)


async def test_head_change_between_commit_and_publish_cannot_repoint_child(review_context):
    r = review_context
    r.provider.side_effect = [r.provider.return_value, PullRequestIdentity(1234, "PR_bound", "org/repo", 77, MOVED_HEAD)]
    response = await send(r.ctx, r.body)
    assert response.status_code == 409, response.text
    assert messages(r.ctx) == []


async def test_lost_queue_response_replays_identical_review_envelope(review_context, monkeypatch):
    r = review_context
    original = r.ctx.child.sqs.send_message
    sent = []

    def lose_once(**kwargs):
        sent.append(kwargs["MessageBody"])
        result = original(**kwargs)
        if len(sent) == 1:
            raise EndpointConnectionError(endpoint_url="https://queue.test")
        return result

    monkeypatch.setattr(r.ctx.child.sqs, "send_message", lose_once)
    assert (await send(r.ctx, r.body)).status_code == 503
    assert (await send(r.ctx, r.body)).status_code == 202
    assert len(sent) == 2 and sent[0] == sent[1]
    assert len(messages(r.ctx)) == 1


@pytest.mark.parametrize("change", ["missing_binding", "stale_claim", "blocked", "unavailable_head", "blank_head"])
async def test_unverifiable_review_context_publishes_nothing(review_context, change):
    r = review_context
    if change == "unavailable_head":
        r.provider.side_effect = PrIdentityError("unavailable")
    elif change == "blank_head":
        r.provider.return_value = PullRequestIdentity(1234, "PR_bound", "org/repo", 77, "")
    else:
        async with r.ctx.session_factory() as db:
            if change == "missing_binding":
                await db.execute(update(OrchestrationPullRequestBinding).values(state="superseded"))
            elif change == "stale_claim":
                await db.execute(update(OrchestrationWorkClaim).values(generation=2))
            else:
                await db.execute(update(OrchestrationExecution).values(status="blocked"))
            await db.commit()
    response = await send(r.ctx, r.body)
    assert response.status_code == 409, response.text
    assert messages(r.ctx) == []
    async with r.ctx.session_factory() as db:
        assert len(list(await db.scalars(select(OrchestrationExecution)))) == 1


async def test_legacy_reviewer_has_no_new_evidence_requirement(graph_context):
    ctx = graph_context
    await enroll(ctx)
    assert (await send(ctx)).status_code == 202
    messages(ctx)
    response = await send(ctx, {**ctx.body, "persona": "reviewer", "request_id": "legacy-review"})
    assert response.status_code == 202, response.text
    envelope = json.loads(messages(ctx)[0]["Body"])
    assert "review_expect" not in envelope
