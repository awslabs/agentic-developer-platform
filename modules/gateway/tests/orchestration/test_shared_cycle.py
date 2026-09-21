"""Real SQL K2 lifecycle using shared-worker assignments, no protected grants."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration.execution_policy import AuthorizationContext, ExecutionPolicy
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationFlow
from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.shared_cycle import SharedCycleServices, registration_target_for_report
from src.shared.models.base import Base
from tests.orchestration import test_merge_controller as merge_protocol
from tests.orchestration import test_review_cycle as protocol
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, store  # noqa: F401

ROLE = "arn:aws:iam::123456789012:role/worker"


@pytest.fixture
async def shared(cycle, monkeypatch):  # noqa: F811
    ctx = cycle
    engine = ctx.factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=[OrchestrationRunReport.__table__]))
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setenv("ADP_SHARED_WORKER_CONTINUATION_ENABLED", "true")
    monkeypatch.setenv("AGENT_WORKER_ROLE_ARN", ROLE)
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-report-signing-key")
    policy = ctx.policy.model_dump(mode="json")
    policy.update(
        schema_version=2,
        user_credentials={
            "permission_mode": "user_configured",
            "lifetime": "provider_managed",
            "aws_role_arns": [ROLE],
            "actions": ["develop", "review", "repair", "merge"],
        },
    )
    ctx.policy = ExecutionPolicy.model_validate(policy)
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        plan.accepted_by_decision_id = ctx.approval.id
        plan.plan_document = {
            "execution_policy": policy,
            "execution_continuation": {
                "contract_version": 1,
                "mode": "shared_worker_role",
                "budget_scope": "authenticated_gateway_calls",
                "accepted_at": datetime.now(UTC).isoformat(),
                "prior_spend_usd": "2",
                "worker_role_arn": ROLE,
                "initial_runs": {
                    ctx.node.id: {
                        "run_id": ctx.root,
                        "attempt": 1,
                        "repo": ctx.binding.repo,
                        "installation_id": 42,
                        "provider_repository_id": 123,
                        "head_sha": ctx.head,
                        "evidence_origin": "owner_reconciled_legacy_delivery",
                        "observed_status": "complete",
                    }
                },
            },
        }
        await db.commit()

    async def authorization(db, **kwargs):
        return AuthorizationContext(
            policy=kwargs["policy"],
            accepted_plan_version=1,
            in_force_plan_version=1,
            principal_id="human",
            member_org_id=protocol.ORG,
            principal_can_authorize=True,
            now=datetime.now(UTC),
            credential_scope=kwargs["credential_scope"],
            observed_spend_usd=ctx.spend,
            observed_attempts=ctx.node.attempts,
            observed_concurrency=0,
            work_owned_by_policy_flow=True,
        )

    monkeypatch.setattr("src.orchestration.shared_policy.resolve_authorization_context", authorization)
    monkeypatch.setattr("src.orchestration.shared_policy._active_count", AsyncMock(return_value=0))
    monkeypatch.setattr(
        "src.orchestration.shared_policy.read_flow_meter", AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(total_usd=ctx.spend))
    )
    monkeypatch.setattr("src.orchestration.flow_budget.reserve_flow_admission", AsyncMock(return_value=SimpleNamespace(admitted=True)))
    monkeypatch.setattr("src.shared.identity.resolver.resolve_user_entity_id", AsyncMock(return_value="sub"))
    monkeypatch.setattr("src.orchestration.shared_cycle.EngineRunStore.from_env", lambda: SimpleNamespace(register=lambda envelope: None))
    ctx.service = SharedCycleServices(ctx.factory, queue=ctx.service.queue, config=ctx.service.config)

    async def finish(run):
        async with ctx.factory() as db:
            row = await db.get(OrchestrationRunReport, run)
            row.terminal_receipt = {"outcome": "complete"}
            await db.commit()

    ctx.finish = finish
    return ctx


@pytest.mark.parametrize(
    "case",
    [
        "test_develop_review_repair_fresh_review_merge_ready",
        "test_changed_head_requires_fresh_review",
        "test_recovery_after_intent_keeps_action_and_run_identity",
        "test_timed_out_send_republishes_same_envelope_without_duplicate_admission",
    ],
)
async def test_shared_transport_preserves_delivery_protocol(shared, case):
    await getattr(protocol, case)(shared)


@pytest.mark.parametrize("flow_state", ["running", "pending"])
async def test_shared_review_merge_observer_and_verified_completion(shared, monkeypatch, flow_state):
    async with shared.factory() as db:
        flow = await db.get(OrchestrationFlow, shared.flow.id)
        flow.state = flow_state
        await db.commit()
    async with merge_protocol.prepared_merge(shared, monkeypatch) as ctx:
        # A shared GitHub account needs no extra implicit non-author approval.
        ctx.remote["rules"] = []
        ctx.remote["protection"] = None
        ctx.remote["reviews"] = []
        ctx.remote["graphql"]["data"]["repository"]["pullRequest"]["reviewDecision"] = None
        await merge_protocol.test_engine_expected_head_merge_then_verified_code_completion(ctx)


async def test_shared_repair_registration_proves_current_dispatch(shared):
    assert (await protocol.tick(shared)).effects_succeeded == 1
    await protocol.review(shared, findings=[{"finding_id": "F1", "summary": "Fix it", "evidence_refs": []}])
    assert (await protocol.tick(shared)).effects_succeeded == 1
    repair = shared.calls[-1]
    async with shared.factory() as db:
        report = await db.get(OrchestrationRunReport, repair["message_id"])
        target = await registration_target_for_report(db, report)
        assert target.run_id == repair["message_id"] and target.accepted_scope == shared.binding.accepted_scope
        # A document cannot redirect a real report capability to another action.
        metadata = json.loads(json.dumps(report.dispatch_metadata))
        metadata["review_cycle_input"]["operation_key"] = "other-operation"
        report.dispatch_metadata = metadata
        from src.orchestration.run_reports import RunReportError

        with pytest.raises(RunReportError, match="unverifiable"):
            await registration_target_for_report(db, report)
