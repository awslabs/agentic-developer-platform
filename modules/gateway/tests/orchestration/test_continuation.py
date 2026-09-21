"""Existing-flow adoption preserves graph/history and refuses moving evidence."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration.compile import ApprovalContext
from src.orchestration.continuation import ContinuationRefusedError, ContinuationRequest, accept_continuation, preview_continuation
from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.state import ActorKind
from src.shared.models.base import Base
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

ROLE = "arn:aws:iam::123456789012:role/agent-worker"
HEAD = "a" * 40


@pytest.fixture
async def legacy(pg_url, monkeypatch):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    models = [
        OrchestrationFlow,
        OrchestrationNode,
        OrchestrationEdge,
        OrchestrationAcceptedPlan,
        OrchestrationDecision,
        OrchestrationPullRequestBinding,
        OrchestrationWorkClaim,
        OrchestrationExecution,
        OrchestrationAction,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[m.__table__ for m in models]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setenv("AGENT_WORKER_ROLE_ARN", ROLE)
    monkeypatch.setattr("src.orchestration.continuation.initialize_meter", AsyncMock(return_value=True))
    now = datetime.now(UTC)
    actor = ApprovalContext(org_id="org", actor_id="owner", actor_role="owner")
    request = ContinuationRequest(
        execution_policy={
            "schema_version": 2,
            "org_id": "org",
            "repository_ids": ["org/repo"],
            "allowed_actions": ["develop", "review", "repair", "merge"],
            "expires_at": now + timedelta(hours=2),
            "user_credentials": {
                "permission_mode": "user_configured",
                "lifetime": "provider_managed",
                "aws_role_arns": [ROLE],
                "actions": ["develop", "review", "repair", "merge"],
            },
            "limits": {"max_spend_usd": "100", "max_attempts_per_node": 8, "max_concurrent_actions": 2, "max_wall_clock_seconds": 3600},
        },
        worker_role_arn=ROLE,
        reconciled_spend_usd="12.34",
        reconciliation_evidence="Usage reconciled through ledger cutoff; PRs and credentials inspected.",
        effects_and_credentials_reconciled=True,
    )
    async with factory() as session:
        flow = OrchestrationFlow(org_id="org", slug="existing", title="Existing", state="running")
        session.add(flow)
        await session.flush()
        nodes = []
        for i, state in enumerate(["awaiting_merge", "passed", "awaiting_gate", "pending", "halted"]):
            node = OrchestrationNode(
                org_id="org",
                flow_id=flow.id,
                epic_ref="E1",
                wave_ref="W1",
                node_ref=f"N{i}",
                kind="gate" if state == "awaiting_gate" else "story",
                state=state,
                title=f"Node {i}",
                issue_ref=str(40 + i),
                attempts=0 if state in {"pending", "awaiting_gate"} else 3,
            )
            session.add(node)
            nodes.append(node)
        await session.flush()
        node = nodes[0]
        plan = OrchestrationAcceptedPlan(
            org_id="org",
            flow_id=flow.id,
            version=2,
            plan_hash="b" * 64,
            plan_document={
                "org_id": "org",
                "flow": "existing",
                "nodes": [{"address": f"existing/E1/W1/{n.node_ref}", "title": n.title} for n in nodes],
            },
        )
        binding = OrchestrationPullRequestBinding(
            org_id="org",
            flow_id=flow.id,
            node_id=node.id,
            attempt=3,
            run_id=attempt_run_id(node.id, 3),
            provider_repository_id=123,
            provider_pr_node_id="PR_7",
            repo="org/repo",
            pr_number=7,
            installation_id=42,
            head_sha=HEAD,
            revision=1,
            role="implementation",
            state="active",
            registered_by="owner",
            registered_by_kind="human",
            accepted_scope=json.dumps({"node": {"kind": node.kind, "title": node.title, "issue_ref": node.issue_ref}}),
        )
        session.add_all([plan, binding, OrchestrationEdge(org_id="org", flow_id=flow.id, from_node_id=nodes[1].id, to_node_id=node.id)])
        await session.commit()
    resolver = SimpleNamespace(resolve=AsyncMock(return_value={"tenant_id": "org", "status": "complete", "arrived_at": now.isoformat()}))
    provider = AsyncMock(return_value=SimpleNamespace(provider_repository_id=123, provider_pr_node_id="PR_7", head_sha=HEAD))
    yield SimpleNamespace(
        factory=factory, flow=flow, nodes=nodes, binding=binding, request=request, actor=actor, resolver=resolver, provider=provider, now=now
    )
    await engine.dispose()


async def preview(fixture, session, request=None):
    return await preview_continuation(
        session,
        flow_id=fixture.flow.id,
        actor=fixture.actor,
        request=request or fixture.request,
        resolver=fixture.resolver,
        resolve_pr=fixture.provider,
        now=fixture.now,
    )


async def accept(fixture, session, request):
    return await accept_continuation(
        session,
        flow_id=fixture.flow.id,
        actor=fixture.actor,
        request=request,
        resolver=fixture.resolver,
        resolve_pr=fixture.provider,
        now=fixture.now,
    )


async def test_preview_preserves_completed_work_and_human_gates(legacy):
    async with legacy.factory() as session:
        result = await preview(legacy, session)
        assert result["ready"]
        assert [n["action"] for n in result["stages"] if n["node_id"] == legacy.nodes[0].id] == ["review_current_revision"]
        assert all(n["action"] == "preserve" for n in result["stages"] if n["state"] in {"passed", "awaiting_gate", "halted"})
        assert not list((await session.scalars(select(OrchestrationExecution))).all())
        assert len(list((await session.scalars(select(OrchestrationAcceptedPlan))).all())) == 1


@pytest.mark.parametrize("run", [None, {"tenant_id": "other", "status": "complete"}, {"tenant_id": "org", "status": "in_progress"}])
async def test_active_or_unverified_worker_never_replaced(legacy, run):
    legacy.resolver.resolve.return_value = run
    async with legacy.factory() as session:
        result = await preview(legacy, session)
        assert result["blockers"][0]["code"] == "prior_worker_active_or_unverified"
        with pytest.raises(ContinuationRefusedError, match="existing worker"):
            await accept(legacy, session, legacy.request.model_copy(update={"expected_snapshot": result["snapshot"]}))
        assert await session.scalar(select(OrchestrationExecution.id)) is None
        assert (await session.get(OrchestrationNode, legacy.nodes[0].id)).attempts == 3


async def test_acceptance_is_attributed_idempotent_and_preserves_graph(legacy):
    async with legacy.factory() as session:
        result = await preview(legacy, session)
        request = legacy.request.model_copy(update={"expected_snapshot": result["snapshot"]})
        receipt = await accept(legacy, session, request)
        await session.commit()
    async with legacy.factory() as session:
        repeated = await accept(legacy, session, request)
        assert repeated["already_accepted"] and repeated["decision_id"] == receipt["decision_id"]
        assert len(list((await session.scalars(select(OrchestrationExecution))).all())) == 1
        execution = await session.scalar(select(OrchestrationExecution))
        assert execution.phase == "awaiting_review" and execution.cycle == 3 and execution.attempts == 0
        assert execution.deadline_at == legacy.now + timedelta(hours=1)
        plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.version == 3))
        marker = plan.plan_document["execution_continuation"]
        assert marker["prior_spend_usd"] == "12.34" and marker["prior_attempts"][legacy.nodes[0].id] == 3
        assert marker["worker_role_arn"] == ROLE
        assert plan.plan_document["execution_policy"]["principal_id"] == "owner"
        assert [
            (n.id, n.state, n.attempts) for n in (await session.scalars(select(OrchestrationNode).order_by(OrchestrationNode.node_ref))).all()
        ] == [(n.id, n.state, n.attempts) for n in legacy.nodes]
        binding = await session.get(OrchestrationPullRequestBinding, legacy.binding.id)
        assert binding.head_sha == HEAD and binding.revision == 1


@pytest.mark.parametrize("mutation", ["head", "title", "attempt", "policy"])
async def test_snapshot_rejects_changed_head_scope_or_authority(legacy, mutation):
    async with legacy.factory() as session:
        result = await preview(legacy, session)
        request = legacy.request.model_copy(update={"expected_snapshot": result["snapshot"]})
        if mutation == "head":
            legacy.provider.return_value.head_sha = "c" * 40
        elif mutation == "policy":
            request = request.model_copy(update={"reconciled_spend_usd": request.reconciled_spend_usd + 1})
        else:
            node = await session.get(OrchestrationNode, legacy.nodes[0].id)
            setattr(node, "title" if mutation == "title" else "attempts", "Changed" if mutation == "title" else 4)
            await session.commit()
        with pytest.raises(ContinuationRefusedError, match="changed"):
            await accept(legacy, session, request)
        assert await session.scalar(select(OrchestrationExecution.id)) is None


async def test_claim_conflict_rolls_back_whole_acceptance(legacy):
    async with legacy.factory() as session:
        session.add(
            OrchestrationWorkClaim(
                org_id="org",
                provider_repository_id=123,
                issue_number=40,
                owner_kind="direct_dispatch",
                owner_ref="other-lane",
                state="held",
                generation=1,
                active_run_id="other-run",
            )
        )
        await session.commit()
        result = await preview(legacy, session)
        with pytest.raises(ContinuationRefusedError, match="claim"):
            await accept(legacy, session, legacy.request.model_copy(update={"expected_snapshot": result["snapshot"]}))
        assert len(list((await session.scalars(select(OrchestrationAcceptedPlan))).all())) == 1
        assert await session.scalar(select(OrchestrationDecision.id)) is None


async def test_service_actor_cannot_accept_authority(legacy):
    legacy.actor = ApprovalContext(org_id="org", actor_id="agent", actor_role="owner", actor_kind=ActorKind.SERVICE)
    async with legacy.factory() as session:
        with pytest.raises(ContinuationRefusedError, match="plan approver"):
            await preview(legacy, session)


async def test_shared_role_configuration_must_match_explicit_acceptance(legacy, monkeypatch):
    monkeypatch.setenv("AGENT_WORKER_ROLE_ARN", ROLE + "-other")
    async with legacy.factory() as session:
        result = await preview(legacy, session)
        assert result["blockers"][0]["code"] == "worker_role_mismatch"
