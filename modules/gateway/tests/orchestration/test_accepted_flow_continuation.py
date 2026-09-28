"""A never-started accepted flow may change transport without resetting authority."""

import copy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from src.orchestration.continuation import ContinuationRefusedError, ContinuationRequest, request_digest
from src.orchestration.execution_policy import ExecutionPolicy, stamp_policy
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.run_reports import OrchestrationRunReport
from src.shared.models.base import Base
from tests.orchestration.test_continuation import accept, legacy, pg_server, pg_url, preview  # noqa: F401


@pytest.fixture
async def accepted(legacy):  # noqa: F811
    ctx = legacy
    engine = ctx.factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[OrchestrationRunReport.__table__]))
    raw = ctx.request.execution_policy.model_dump(mode="json")
    raw.pop("user_credentials")
    raw["schema_version"] = 1
    raw["evaluation_acceptance"] = {"existing/E1/W1/prerequisite": "machine", "existing/E1/W1/final": "machine"}
    ctx.original = stamp_policy(ExecutionPolicy.model_validate(raw), principal_id="original-sub", org_id="org")
    ctx.started = ctx.now - timedelta(minutes=30)
    async with ctx.factory() as db:
        await db.execute(delete(OrchestrationPullRequestBinding))
        for node in await db.scalars(select(OrchestrationNode)):
            node.attempts = 0
            if node.kind != "gate":
                node.state = "ready"
            if node.id == ctx.nodes[4].id:
                node.kind = "eval"
        decision = OrchestrationDecision(
            org_id="org",
            flow_id=ctx.flow.id,
            kind="plan_accepted",
            actor_id="original-sub",
            actor_kind="human",
            actor_role="owner",
            created_at=ctx.started,
        )
        db.add(decision)
        await db.flush()
        plan = await db.scalar(select(OrchestrationAcceptedPlan))
        document = copy.deepcopy(plan.plan_document)
        document["execution_policy"] = ctx.original.model_dump(mode="json")
        plan.plan_document = document
        plan.accepted_by_decision_id = decision.id
        plan.created_at = ctx.started
        await db.commit()
        ctx.original_decision = decision.id
    request = ctx.request.model_dump(mode="json")
    request["preserve_accepted_policy"] = True
    request["execution_policy"]["evaluation_acceptance"] = raw["evaluation_acceptance"]
    ctx.request = ContinuationRequest.model_validate(request)
    return ctx


async def test_preserved_acceptance_retains_limits_deadline_and_evaluation_gates(accepted, monkeypatch):
    ctx = accepted
    meter = AsyncMock(return_value=True)
    monkeypatch.setattr("src.orchestration.continuation.initialize_meter", meter)
    async with ctx.factory() as db:
        previewed = await preview(ctx, db)
        assert previewed["ready"], previewed["blockers"]
        request = ctx.request.model_copy(update={"expected_snapshot": previewed["snapshot"]})
        receipt = await accept(ctx, db, request)
        await db.commit()
    async with ctx.factory() as db:
        repeated = await accept(ctx, db, request)
        assert repeated["already_accepted"] and repeated["decision_id"] == receipt["decision_id"]
        plan = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.superseded_at.is_(None)))
        policy = ExecutionPolicy.model_validate(plan.plan_document["execution_policy"])
        marker = plan.plan_document["execution_continuation"]
        assert policy.limits == ctx.original.limits
        assert policy.expires_at == ctx.original.expires_at
        assert policy.evaluation_acceptance == ctx.original.evaluation_acceptance
        assert policy.allowed_actions == ctx.original.allowed_actions and "evaluate" not in policy.allowed_actions
        assert policy.principal_id == "owner" and policy.user_credentials == ctx.request.execution_policy.user_credentials
        assert marker["accepted_at"] == ctx.started.isoformat()
        assert marker["continued_at"] == ctx.now.isoformat()
        assert marker["preserved_acceptance_decision_id"] == ctx.original_decision
        assert marker["preserved_policy_hash"] == ctx.original.policy_hash
        assert marker["preserved_plan_version"] == 2
        assert "delivery_mode" not in marker
        assert marker["prior_spend_usd"] == "12.34" and not any(marker["prior_attempts"].values())
        assert not marker["initial_runs"]
        assert await db.scalar(select(OrchestrationExecution.id)) is None
        assert (await db.get(OrchestrationNode, ctx.nodes[2].id)).state == "awaiting_gate"
        evaluation = await db.get(OrchestrationNode, ctx.nodes[4].id)
        assert evaluation.kind == "eval" and evaluation.state == "ready" and evaluation.attempts == 0
    meter.assert_awaited_once()
    ctx.resolver.resolve.assert_not_awaited()
    ctx.provider.assert_not_awaited()


async def test_code_only_delivery_is_explicit_and_snapshot_bound(accepted):
    ctx = accepted
    async with ctx.factory() as db:
        prior = await preview(ctx, db)
        request = ctx.request.model_copy(update={"delivery_mode": "code_only", "expected_snapshot": prior["snapshot"]})
        with pytest.raises(ContinuationRefusedError, match="changed"):
            await accept(ctx, db, request)
        result = await preview(ctx, db, request)
        assert result["ready"] and result["delivery_mode"] == "code_only"
        await accept(ctx, db, request.model_copy(update={"expected_snapshot": result["snapshot"]}))
        plan = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.superseded_at.is_(None)))
        assert plan.plan_document["execution_continuation"]["delivery_mode"] == "code_only"
        assert plan.plan_document["execution_policy"]["evaluation_acceptance"] == ctx.original.model_dump(mode="json")["evaluation_acceptance"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("limits.max_spend_usd", "200"),
        ("limits.max_attempts_per_node", 9),
        ("limits.max_concurrent_actions", 3),
        ("limits.max_wall_clock_seconds", 7200),
        ("repository_ids", ["org/other"]),
        ("team_ids", ["team"]),
        ("human_gates", ["merge"]),
        ("evaluation_acceptance", {}),
        ("allowed_actions", ["review", "repair", "merge"]),
        ("expires_at", "2099-01-01T00:00:00Z"),
    ],
)
async def test_transport_adoption_cannot_change_any_accepted_bound(accepted, field, value):
    request = accepted.request.model_dump(mode="json")
    target = request["execution_policy"]
    if "." in field:
        group, field = field.split(".")
        target = target[group]
    target[field] = value
    if field == "allowed_actions":
        request["execution_policy"]["user_credentials"]["actions"] = value
    request = ContinuationRequest.model_validate(request)
    async with accepted.factory() as db:
        result = await preview(accepted, db, request)
        assert result["blockers"][0]["code"] == "accepted_policy_changed"
        with pytest.raises(ContinuationRefusedError, match="retain every"):
            await accept(accepted, db, request.model_copy(update={"expected_snapshot": result["snapshot"]}))


@pytest.mark.parametrize("evidence", ["attempt", "dispatch", "policy_hash", "service_acceptance", "elapsed_clock"])
async def test_existing_work_or_unverifiable_acceptance_cannot_be_adopted(accepted, evidence):
    ctx = accepted
    async with ctx.factory() as db:
        if evidence == "attempt":
            node = await db.get(OrchestrationNode, ctx.nodes[0].id)
            node.attempts = 1
        elif evidence == "dispatch":
            db.add(
                OrchestrationDecision(
                    org_id="org",
                    flow_id=ctx.flow.id,
                    node_id=ctx.nodes[0].id,
                    kind="node_dispatched",
                    actor_id="engine",
                    actor_kind="service",
                    actor_role="engine",
                )
            )
        elif evidence == "policy_hash":
            plan = await db.scalar(select(OrchestrationAcceptedPlan))
            document = copy.deepcopy(plan.plan_document)
            document["execution_policy"]["policy_hash"] = "bad"
            plan.plan_document = document
        elif evidence == "service_acceptance":
            decision = OrchestrationDecision(
                org_id="org", flow_id=ctx.flow.id, kind="plan_accepted", actor_id="original-sub", actor_kind="service", actor_role="owner"
            )
            db.add(decision)
            await db.flush()
            plan = await db.scalar(select(OrchestrationAcceptedPlan))
            plan.accepted_by_decision_id = decision.id
        else:
            ctx.now += timedelta(hours=1)
        await db.commit()
        result = await preview(ctx, db)
        expected = (
            "accepted_flow_already_started"
            if evidence in {"attempt", "dispatch"}
            else "wall_clock_limit_exceeded"
            if evidence == "elapsed_clock"
            else "accepted_policy_unverifiable"
        )
        assert result["blockers"][0]["code"] == expected


async def test_worker_evidence_after_preview_invalidates_acceptance(accepted):
    ctx = accepted
    async with ctx.factory() as db:
        result = await preview(ctx, db)
        request = ctx.request.model_copy(update={"expected_snapshot": result["snapshot"]})
        async with ctx.factory() as other:
            other.add(
                OrchestrationDecision(
                    org_id="org",
                    flow_id=ctx.flow.id,
                    node_id=ctx.nodes[0].id,
                    kind="node_dispatched",
                    actor_id="engine",
                    actor_kind="service",
                    actor_role="engine",
                )
            )
            await other.commit()
        with pytest.raises(ContinuationRefusedError, match="changed"):
            await accept(ctx, db, request)
        assert len(list(await db.scalars(select(OrchestrationAcceptedPlan)))) == 1


@pytest.mark.parametrize("kind", ["expired_report", "superseded_binding", "released_claim", "concluded_execution"])
async def test_even_terminal_history_prevents_pristine_adoption(accepted, kind):
    ctx = accepted
    async with ctx.factory() as db:
        if kind == "expired_report":
            db.add(
                OrchestrationRunReport(
                    run_id="prior-run",
                    credential_hash="a" * 64,
                    org_id="org",
                    flow_id=ctx.flow.id,
                    node_id=ctx.nodes[0].id,
                    attempt=1,
                    persona="developer",
                    repo="org/repo",
                    installation_id=42,
                    dispatch_metadata={},
                    terminal_receipt={"outcome": "complete"},
                    expires_at=ctx.now - timedelta(hours=1),
                )
            )
        elif kind == "superseded_binding":
            values = {column.name: getattr(ctx.binding, column.name) for column in OrchestrationPullRequestBinding.__table__.columns}
            values["state"] = "superseded"
            db.add(OrchestrationPullRequestBinding(**values))
        elif kind == "released_claim":
            db.add(
                OrchestrationWorkClaim(
                    org_id="org",
                    provider_repository_id=123,
                    issue_number=40,
                    owner_kind="engine_flow",
                    owner_ref=ctx.flow.id,
                    state="released",
                    generation=1,
                    active_run_id="prior-run",
                )
            )
        else:
            db.add(
                OrchestrationExecution(
                    org_id="org",
                    flow_id=ctx.flow.id,
                    node_id=ctx.nodes[0].id,
                    cycle=1,
                    phase="concluded",
                    status="concluded",
                    accepted_plan_version=2,
                    claim_id="prior-claim",
                    claim_generation=1,
                )
            )
        await db.commit()
        result = await preview(ctx, db)
        assert result["blockers"][0]["code"] == "accepted_flow_already_started"


async def test_preserved_adoption_requires_opt_in_and_an_existing_policy(accepted):
    ctx = accepted
    # Dropping the map makes the legacy request structurally valid; it still
    # cannot replace an accepted policy through the older continuation path.
    request = ctx.request.model_dump(mode="json")
    request["preserve_accepted_policy"] = False
    request["execution_policy"]["evaluation_acceptance"] = {}
    async with ctx.factory() as db:
        result = await preview(ctx, db, ContinuationRequest.model_validate(request))
        assert result["blockers"][0]["code"] == "already_governed"
        plan = await db.scalar(select(OrchestrationAcceptedPlan))
        document = copy.deepcopy(plan.plan_document)
        document.pop("execution_policy")
        plan.plan_document = document
        await db.commit()
        result = await preview(ctx, db)
        assert result["blockers"][0]["code"] == "accepted_policy_unverifiable"


def test_legacy_request_hash_retains_pre_adoption_shape():
    from datetime import UTC, datetime

    from src.orchestration.compile import ApprovalContext
    from src.orchestration.continuation import digest
    from tests.orchestration.test_continuation import ROLE

    request = ContinuationRequest(
        execution_policy={
            "schema_version": 2,
            "org_id": "org",
            "repository_ids": ["org/repo"],
            "allowed_actions": ["review", "repair"],
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
            "limits": {"max_spend_usd": "100", "max_attempts_per_node": 2, "max_concurrent_actions": 1, "max_wall_clock_seconds": 3600},
            "user_credentials": {
                "permission_mode": "user_configured",
                "lifetime": "provider_managed",
                "aws_role_arns": [ROLE],
                "actions": ["review", "repair"],
            },
        },
        worker_role_arn=ROLE,
        reconciled_spend_usd="0",
        reconciliation_evidence="No previous work",
        effects_and_credentials_reconciled=True,
    )
    actor = ApprovalContext(org_id="org", actor_id="owner", actor_role="owner")
    old_request = request.model_dump(mode="json", exclude={"expected_snapshot", "preserve_accepted_policy", "accept_draft_policy", "delivery_mode"})
    assert request_digest(request, actor) == digest(
        {"request": old_request, "actor": "owner", "org": "org", "budget_scope": "authenticated_gateway_calls"}
    )
