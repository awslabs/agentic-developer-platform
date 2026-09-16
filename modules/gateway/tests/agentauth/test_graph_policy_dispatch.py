"""Accepted policy enforced through the public delegated dispatch endpoint."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.orchestration.execution_policy import Action, ExecutionPolicy, PolicyLimits, stamp_policy
from src.orchestration.models import OrchestrationAcceptedPlan
from src.shared.models.onboarding import TenantMembership
from tests.agentauth.test_graph_dispatch import (  # noqa: F401
    child_dispatch,
    engine,
    enroll,
    messages,
    node_state,
    send,
    session,
    session_factory,
    store,
)
from tests.agentauth.test_graph_dispatch import graph_context as graph_context_fixture
from tests.orchestration.test_policy_admission import (  # noqa: F401
    healthy_policy_reservations,
    policy_budget_initializers,
)

graph_context = graph_context_fixture


async def accept(ctx, *, actions=None, human_gates=None):
    policy = stamp_policy(
        ExecutionPolicy(
            org_id="tenant",
            repository_ids=["org/repo"],
            allowed_actions=actions or [Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.MERGE],
            human_gates=human_gates or [],
            expires_at=datetime.now(UTC) + timedelta(hours=3),
            limits=PolicyLimits(max_wall_clock_seconds=7200, max_spend_usd=100, max_attempts_per_node=3, max_concurrent_actions=3),
        ),
        principal_id="human",
        org_id="tenant",
    )
    async with ctx.session_factory() as db:
        db.add(TenantMembership(user_id="human", tenant_id="tenant", role="org_admin"))
        db.add(
            OrchestrationAcceptedPlan(
                org_id="tenant",
                flow_id=ctx.flow.id,
                version=1,
                accepted_by_decision_id=ctx.approval.id,
                plan_hash="test-policy",
                plan_document={"execution_policy": policy.model_dump(mode="json")},
            )
        )
        await db.commit()


async def test_policy_allows_developer_then_reviewer_and_idempotent_replay(graph_context):
    ctx = graph_context
    await accept(ctx)
    await enroll(ctx)
    first = await send(ctx)
    assert first.status_code == 202, first.text
    replay = await send(ctx)
    assert replay.status_code == 202, replay.text
    assert replay.json() == first.json()
    review = await send(ctx, {**ctx.body, "persona": "reviewer", "request_id": "review-policy"})
    assert review.status_code == 202, review.text
    assert await node_state(ctx) == ("running", 1)
    assert len(messages(ctx)) == 2


async def test_policy_action_refusal_preserves_attempt_and_cancels_unpublished_child(graph_context):
    ctx = graph_context
    await accept(ctx, actions=[Action.REVIEW])
    await enroll(ctx)
    response = await send(ctx)
    assert response.status_code == 409, response.text
    assert "action_not_permitted" in response.text
    assert await node_state(ctx) == ("ready", 0)
    assert messages(ctx) == []
    assert ctx.store.authority.active_dispatch_count(grant_id=f"grant:{ctx.child.invocation}:1", tenant_id="tenant") == 0


async def test_human_merge_gate_refuses_unscopable_developer_token(graph_context):
    ctx = graph_context
    await accept(ctx, human_gates=[Action.MERGE])
    await enroll(ctx)
    response = await send(ctx)
    assert response.status_code == 409, response.text
    assert "credential_scope_unavailable" in response.text
    assert await node_state(ctx) == ("ready", 0)
    assert messages(ctx) == []


async def test_role_revocation_stops_replay_before_publication(graph_context, monkeypatch):
    ctx = graph_context
    await accept(ctx)
    await enroll(ctx)
    original = ctx.child.service._publish

    def unavailable(*_):
        raise RuntimeError("publisher unavailable before send")

    monkeypatch.setattr(ctx.child.service, "_publish", unavailable)
    import pytest

    with pytest.raises(RuntimeError, match="publisher unavailable"):
        await send(ctx)
    assert await node_state(ctx) == ("running", 1)
    assert messages(ctx) == []
    async with ctx.session_factory() as db:
        membership = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "human"))
        membership.role = "member"
        await db.commit()
    monkeypatch.setattr(ctx.child.service, "_publish", original)
    response = await send(ctx)
    assert response.status_code == 409, response.text
    assert "role_revoked" in response.text
    assert messages(ctx) == []
    assert await node_state(ctx) == ("running", 1)
