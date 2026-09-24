"""A real shared reviewer keeps its original identity through a future-wave edit."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration.compile import ApprovalContext
from src.orchestration.continuation import digest
from src.orchestration.execution_policy import AuthorizationContext, ExecutionPolicy, stamp_policy
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationAmendmentRequest, OrchestrationEdge, OrchestrationFlow, OrchestrationNode
from src.orchestration.proposal import LoopProposal
from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.shared_cycle import validate_current_report_assignment
from src.orchestration.shared_policy import authorize_shared_model
from src.orchestration.wave_amendment import WaveDependencyRequest, accept_wave_dependencies, preview_wave_dependencies
from src.shared.models.base import Base
from tests.orchestration import test_merge_controller as merge_protocol
from tests.orchestration import test_review_cycle as protocol
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


@pytest.fixture
async def amendable(shared, monkeypatch):  # noqa: F811
    ctx = shared
    async with ctx.factory.kw["bind"].begin() as connection:
        await connection.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[OrchestrationEdge.__table__, OrchestrationAmendmentRequest.__table__])
        )
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.node.id)
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        evaluation = OrchestrationNode(
            org_id=node.org_id,
            flow_id=node.flow_id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="EV",
            kind="eval",
            state="pending",
            title="Independent checkpoint",
            attempts=0,
        )
        future = OrchestrationNode(
            org_id=node.org_id,
            flow_id=node.flow_id,
            epic_ref="E1",
            wave_ref="W2",
            node_ref="G",
            kind="gate",
            state="pending",
            title="Future wave",
            attempts=0,
        )
        db.add_all([evaluation, future])
        await db.flush()
        db.add_all(
            [
                OrchestrationEdge(org_id=node.org_id, flow_id=node.flow_id, from_node_id=a, to_node_id=b)
                for a, b in [(node.id, evaluation.id), (evaluation.id, future.id)]
            ]
        )
        raw = ctx.policy.model_dump(mode="json", exclude={"policy_id", "policy_hash", "principal_id"})
        ctx.policy = stamp_policy(ExecutionPolicy.model_validate(raw), principal_id="human", org_id=node.org_id)
        proposal = LoopProposal(
            flow_slug="cycle",
            title="Cycle",
            org_id=node.org_id,
            spec_revision="r1",
            nodes=[
                dict(address=f"cycle/{n.epic_ref}/{n.wave_ref}/{n.node_ref}", kind=n.kind, title=n.title, issue_ref=n.issue_ref)
                for n in [node, evaluation, future]
            ],
            edges=[dict(from_address=a, to_address=b) for a, b in [("cycle/E1/W1/N1", "cycle/E1/W1/EV"), ("cycle/E1/W1/EV", "cycle/E1/W2/G")]],
            execution_policy=ctx.policy,
        )
        document = proposal.model_dump(mode="json")
        document["execution_continuation"] = {**plan.plan_document["execution_continuation"], "delivery_mode": "code_only"}
        plan.plan_document, plan.plan_hash = document, digest(document)
        await db.commit()
        ctx.request = WaveDependencyRequest(
            expected_plan_version=1,
            expected_plan_hash=plan.plan_hash,
            added_edges=[dict(from_address="cycle/E1/W1/N1", to_address="cycle/E1/W2/G")],
            reason="Add explicit prerequisite for the future wave",
        )
        ctx.actor = ApprovalContext(org_id=node.org_id, actor_id="human", actor_role="org_admin")

    async def authorization(db, **kwargs):
        return AuthorizationContext(
            policy=kwargs["policy"],
            accepted_plan_version=kwargs["plan_version"],
            in_force_plan_version=kwargs["plan_version"],
            principal_id="human",
            member_org_id=protocol.ORG,
            principal_can_authorize=True,
            now=datetime.now(UTC),
            credential_scope=kwargs["credential_scope"],
            observed_spend_usd=ctx.spend,
            observed_attempts=1,
            observed_concurrency=0,
            work_owned_by_policy_flow=True,
        )

    monkeypatch.setattr("src.orchestration.shared_policy.resolve_authorization_context", authorization)
    return ctx


async def amend(ctx):
    async with ctx.factory() as db:
        flow = await db.get(OrchestrationFlow, ctx.flow.id)
        flow.execution_paused = True
        await db.commit()
        preview = await preview_wave_dependencies(db, flow_id=flow.id, actor=ctx.actor, request=ctx.request)
        request = ctx.request.model_copy(update={"expected_snapshot": preview["snapshot"]})
        result = await accept_wave_dependencies(db, flow_id=flow.id, actor=ctx.actor, request=request)
        await db.commit()
        assert result["plan_version"] == 2 and flow.execution_paused


async def test_active_reviewer_model_report_and_fresh_review_survive_amendment(amendable):
    ctx = amendable
    assert (await protocol.tick(ctx)).effects_succeeded == 1
    reviewer = ctx.calls[-1]["message_id"]
    await amend(ctx)
    async with ctx.factory() as db:
        assignment = await db.get(OrchestrationRunReport, reviewer)
        execution, identity = await validate_current_report_assignment(db, assignment)
        assert identity.accepted_plan_version == execution.accepted_plan_version == 1
        from src.orchestration.stall import _continuation_clock

        current, since = await _continuation_clock(db, SimpleNamespace(org_id=ctx.actor.org_id, flow_id=ctx.flow.id, node_id=ctx.node.id, attempts=1))
        assert current and since == assignment.created_at
        await authorize_shared_model(db, assignment)
        (await db.get(OrchestrationFlow, ctx.flow.id)).execution_paused = False
        await db.commit()
    await protocol.review(ctx, approve=True)
    ctx.head = "b" * 40  # A later push still requires a fresh review of that head.
    assert (await protocol.tick(ctx)).effects_succeeded == 1
    assert len(ctx.calls) == 2 and ctx.calls[-1]["execution_continuation"]["accepted_plan_version"] == 1


async def test_reviewer_merge_reconciles_while_paused_after_amendment(amendable, monkeypatch):
    async with merge_protocol.prepared_merge(amendable, monkeypatch) as ctx:
        await amend(ctx)
        ctx.merge_remote()
        await merge_protocol.tick(ctx)
        execution, claim, node, _ = await protocol.state(ctx)
        assert node.state == "passed" and execution.phase == "concluded"
        assert execution.accepted_plan_version == 1 and claim.generation == 5
        assert len(ctx.calls) == 1 and ctx.mutations == []


@pytest.mark.parametrize("amend_first", [False, True])
async def test_recovery_approval_survives_on_either_side_of_amendment(amendable, amend_first):
    from src.orchestration.review_recovery import ReviewRecoveryRequest, request_review_recovery

    ctx = amendable
    assert (await protocol.tick(ctx)).effects_succeeded == 1
    run = ctx.calls[-1]["message_id"]
    resolver = SimpleNamespace(
        read_current=AsyncMock(return_value={"tenant_id": ctx.actor.org_id, "status": "failed", "arrived_at": "2026-01-01T00:00:00Z"})
    )
    if amend_first:
        await amend(ctx)
    async with ctx.factory() as db:
        row = await db.get(OrchestrationRunReport, run)
        row.block_code = "stale_head"
        await db.commit()
        body = ReviewRecoveryRequest(
            expected_attempt=1,
            expected_plan_version=2 if amend_first else 1,
            expected_run_id=run,
            expected_head_sha=ctx.head,
            reason="Recover only the positively exited reviewer",
        )
        args = dict(org_id=ctx.actor.org_id, node_id=ctx.node.id, actor_id="human", actor_role="org_admin", resolver=resolver)
        preview = await request_review_recovery(db, **args, request=body)
        await request_review_recovery(db, **args, request=body.model_copy(update={"expected_snapshot": preview["snapshot"]}), accept=True)
        await db.commit()
    if not amend_first:
        await amend(ctx)
    async with ctx.factory() as db:
        (await db.get(OrchestrationFlow, ctx.flow.id)).execution_paused = False
        await db.commit()
    assert (await protocol.tick(ctx)).effects_succeeded == 1
    assert len(ctx.calls) == 2
