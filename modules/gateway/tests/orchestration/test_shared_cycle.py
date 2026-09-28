"""Real SQL K2 lifecycle using shared-worker assignments, no protected grants."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration.execution_policy import AuthorizationContext, ExecutionPolicy
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationFlow
from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.shared_cycle import SharedCycleServices
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
        ctx.merge_remote()  # The reviewer has already performed the merge.
        await merge_protocol.tick(ctx)
        assert (await protocol.state(ctx))[2].state == "passed"
        assert ctx.mutations == []
        assert len(ctx.calls) == 1


async def test_owned_reviewer_blocker_does_not_dispatch_another_paid_run(shared):
    assert (await protocol.tick(shared)).effects_succeeded == 1
    await protocol.review(shared, findings=[{"finding_id": "F1", "summary": "External dependency unavailable", "evidence_refs": []}])
    for _ in range(3):
        assert (await protocol.tick(shared)).effects_succeeded == 0
    execution, _, _, _ = await protocol.state(shared)
    assert execution.block_detail == "reviewer_delivery_blocked"
    assert len(shared.calls) == 1


async def test_capacity_waits_do_not_consume_attempts_then_dispatch_once(shared, monkeypatch):
    active = AsyncMock(return_value=shared.policy.limits.max_concurrent_actions)
    monkeypatch.setattr("src.orchestration.shared_policy._active_count", active)
    before = await protocol.state(shared)
    for _ in range(3):
        result = await protocol.tick(shared)
        assert result.effects_succeeded == 0 and result.effects_uncertain == 0
    after = await protocol.state(shared)
    assert not shared.calls and not after[3]
    assert after[0].attempts == before[0].attempts and after[2].attempts == before[2].attempts
    active.return_value = 0
    assert (await protocol.tick(shared)).effects_succeeded == 1
    assert len(shared.calls) == 1


async def test_uncertain_dispatch_waits_for_capacity_without_consuming_retries(shared, monkeypatch):
    queue = shared.service.queue
    original = queue.send_message
    queue.send_message = lambda **kwargs: (_ for _ in ()).throw(TimeoutError("response lost"))
    assert (await protocol.tick(shared)).effects_uncertain == 1
    before = await protocol.state(shared)
    active = AsyncMock(return_value=shared.policy.limits.max_concurrent_actions)
    monkeypatch.setattr("src.orchestration.shared_policy._active_count", active)
    for _ in range(3):
        assert (await protocol.tick(shared)).effects_uncertain == 0
    assert (await protocol.state(shared))[0].attempts == before[0].attempts
    assert len((await protocol.state(shared))[3]) == 1
    active.return_value = 0
    queue.send_message = original
    assert (await protocol.tick(shared)).effects_succeeded == 1
    assert len(shared.calls) == 1


@pytest.mark.parametrize("paused", [False, True])
async def test_started_successor_settles_uncertain_dispatch_without_republishing(shared, paused):
    queue = shared.service.queue
    original = queue.send_message

    def lost_response(**kwargs):
        original(**kwargs)
        raise TimeoutError("response lost")

    queue.send_message = lost_response
    assert (await protocol.tick(shared)).effects_uncertain == 1
    async with shared.factory() as db:
        row = await db.get(OrchestrationRunReport, shared.calls[-1]["message_id"])
        row.worker_receipt = {"started": True}
        (await db.get(OrchestrationFlow, shared.flow.id)).execution_paused = paused
        await db.commit()
    await protocol.tick(shared)
    after = await protocol.state(shared)
    assert after[0].pending_action_key is None
    assert len(shared.calls) == 1


async def test_engine_does_not_merge_when_owned_reviewer_has_not_delivered(shared, monkeypatch):
    async with merge_protocol.prepared_merge(shared, monkeypatch) as ctx:
        result = await merge_protocol.tick(ctx)
        assert result.effects_attempted == 0
        assert (await protocol.state(ctx))[0].block_detail == "reviewer_merge_not_delivered"
        assert ctx.mutations == [] and len(ctx.calls) == 1


@pytest.mark.parametrize("paused", [False, True])
async def test_reviewer_merge_completes_code_only_story_without_another_worker(shared, monkeypatch, paused):
    async with shared.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, shared.plan.id)
        plan.plan_document = {
            **plan.plan_document,
            "execution_continuation": {**plan.plan_document["execution_continuation"], "delivery_mode": "code_only"},
        }
        await db.commit()
    async with merge_protocol.prepared_merge(shared, monkeypatch) as ctx:
        async with ctx.factory() as db:
            (await db.get(OrchestrationFlow, ctx.flow.id)).execution_paused = paused
            await db.commit()
        ctx.merge_remote()
        await merge_protocol.tick(ctx)
        execution, _, node, _ = await protocol.state(ctx)
        assert node.state == "passed" and execution.phase == "concluded"
        assert ctx.mutations == [] and len(ctx.calls) == 1


@pytest.mark.parametrize("terminal", [None, {"outcome": "failed"}])
async def test_merge_is_reconciled_when_reviewer_terminal_report_is_missing_or_failed(shared, monkeypatch, terminal):
    async with merge_protocol.prepared_merge(shared, monkeypatch) as ctx:
        async with ctx.factory() as db:
            row = await db.get(OrchestrationRunReport, ctx.calls[-1]["message_id"])
            row.terminal_receipt = terminal
            row.review_receipt = {"recorded": True}
            await db.commit()
        if terminal is None:
            result = await merge_protocol.tick(ctx)
            assert result.effects_attempted == 0
            assert (await protocol.state(ctx))[2].state == "running"
        ctx.merge_remote()
        await merge_protocol.tick(ctx)
        assert (await protocol.state(ctx))[2].state == "passed"
        assert ctx.mutations == [] and len(ctx.calls) == 1


@pytest.mark.parametrize("authority_enabled", ["true", "false"])
async def test_accepted_shared_flow_selects_shared_transport_during_authority_rollout(shared, monkeypatch, authority_enabled):
    from src.orchestration.review_cycle_dispatch import cycle_services

    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", authority_enabled)
    context = SimpleNamespace(identity=shared.identity, execution=shared.execution)
    service = await cycle_services(shared.factory, context)
    assert isinstance(service, SharedCycleServices)
    async with shared.factory() as db:
        facts = await service.authority_context(db, context, shared.node, shared.binding, shared.root, protocol.Action.REVIEW)
    assert facts[0]["evidence_origin"]["S"] == "owner_reconciled_legacy_delivery"


async def test_disabled_shared_transport_blocks_before_protected_lookup_or_dispatch(shared, monkeypatch):
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_SHARED_WORKER_CONTINUATION_ENABLED", "false")
    shared.service = None
    result = await protocol.tick(shared)
    assert result.effects_attempted == 0
    assert (await protocol.state(shared))[0].block_detail == "shared_worker_continuation_disabled"
    assert not shared.calls


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("unknown_mode", "continuation_mode_unrecognized"),
        ("malformed", "continuation_mode_unrecognized"),
        ("version", "continuation_plan_changed"),
        ("unattributed", "continuation_acceptance_unverifiable"),
    ],
)
async def test_transport_selection_never_downgrades_unverifiable_plans(shared, monkeypatch, mutation, reason):
    from src.orchestration.review_cycle import CycleBlockedError
    from src.orchestration.review_cycle_dispatch import cycle_services

    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    async with shared.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, shared.plan.id)
        document = dict(plan.plan_document)
        if mutation == "unknown_mode":
            document["execution_continuation"] = {"mode": "unknown", "contract_version": 1}
        elif mutation == "malformed":
            document["execution_continuation"] = "shared_worker_role"
        elif mutation == "version":
            plan.version += 1
        else:
            plan.accepted_by_decision_id = None
        plan.plan_document = document
        await db.commit()
    with pytest.raises(CycleBlockedError, match=reason):
        await cycle_services(shared.factory, SimpleNamespace(identity=shared.identity, execution=shared.execution))


async def test_shared_feature_flag_does_not_reinterpret_protected_flow(cycle, monkeypatch):  # noqa: F811
    from src.orchestration.review_cycle_dispatch import ReviewCycleServices, cycle_services

    monkeypatch.setenv("ADP_SHARED_WORKER_CONTINUATION_ENABLED", "true")
    service = await cycle_services(cycle.factory, SimpleNamespace(identity=cycle.identity, execution=cycle.execution))
    assert type(service) is ReviewCycleServices


async def test_production_router_dispatches_and_settles_shared_review_with_authority_enabled(shared, monkeypatch):
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setattr("src.orchestration.shared_cycle._get_sqs_client", lambda region: shared.service.queue)
    monkeypatch.setattr("src.orchestration.shared_cycle.DispatchPassConfig.from_env", lambda: shared.service.config)
    routed = SimpleNamespace(**vars(shared))
    routed.service = None
    assert (await protocol.tick(routed)).effects_succeeded == 1
    assert len(shared.calls) == 1
    await protocol.review(shared, approve=True)
    await protocol.tick(routed)
    assert (await protocol.state(shared))[0].phase == "merge_ready"


async def test_merge_observer_uses_shared_receipts_during_authority_rollout(shared, monkeypatch):
    async with merge_protocol.prepared_merge(shared, monkeypatch) as ctx:
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        ctx.merge_services.authority = None
        ctx.remote["rules"] = []
        ctx.remote["protection"] = None
        ctx.remote["reviews"] = []
        ctx.merge_remote()
        await merge_protocol.tick(ctx)
        assert (await protocol.state(ctx))[2].state == "passed"
        assert ctx.mutations == []
