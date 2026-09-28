"""Real K2 attempts and PostgreSQL admission locks at shared-worker limits."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agentauth.model_identity import AgentModelIdentityMiddleware
from src.orchestration.dispatch_pass import _build_envelope, attempt_run_id
from src.orchestration.execution_policy import ExecutionPolicy
from src.orchestration.genesis import resolve_engine_genesis
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.run_reports import OrchestrationRunReport, prepare_run_report
from src.orchestration.shared_policy import _active_count, authorize_shared_dispatch
from src.shared.schemas.auth import TokenContext
from tests.orchestration import test_review_cycle as protocol
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


async def set_attempt_limit(ctx, limit):
    policy = ctx.policy.model_dump(mode="json")
    policy["limits"]["max_attempts_per_node"] = limit
    ctx.policy = ExecutionPolicy.model_validate(policy)
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        plan.plan_document = {**plan.plan_document, "execution_policy": policy}
        await db.commit()


async def envelope_for(ctx, db, node):
    genesis = await resolve_engine_genesis(db, org_id=node.org_id, decision_id=ctx.approval.id)
    envelope = _build_envelope(
        node=node,
        genesis=genesis,
        graph_address="cycle/E1/W1/N1",
        installation_id=42,
        issue=int(node.issue_ref),
        config=ctx.service.config,
        user_id="human",
        cognito_sub="sub",
    )
    envelope["source_ref"]["provider_repository_id"] = 123
    return envelope


async def model_call(ctx, credential, monkeypatch):
    # Exercise actual report authentication, current K2 assignment, shared policy,
    # and middleware. Provider pricing and reservation have separate real tests.
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: ctx.factory)
    monkeypatch.setattr("src.agentauth.model_identity.quote_request", AsyncMock(return_value=SimpleNamespace(total_usd=Decimal("0.01"))))
    monkeypatch.setattr("src.agentauth.model_identity.revalidate_quote", AsyncMock())
    token = TokenContext(
        user_id="iam-agent:scaledjob-worker",
        agent_registry_id="scaledjob-worker",
        org_id="__platform__",
        team_id="",
        department_id="",
        account_type="service",
        auth_source="iam",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "server": ("gateway.test", 443),
        "query_string": b"",
        "path": "/v1/messages",
        "state": {"token_context": token},
        "headers": [(b"x-adp-report-credential", credential.encode())],
    }
    received, sent = [], []

    async def receive():
        return {"type": "http.request", "body": b'{"model":"test-model"}', "more_body": False}

    async def send(message):
        sent.append(message)

    async def provider(scope, receive, send):
        received.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    await AgentModelIdentityMiddleware(provider)(scope, receive, send)
    assert sent[0]["status"] == 200, sent
    assert len(received) == 1 and token._policy_flow_target is not None


@pytest.mark.parametrize("limit", [1, 2])
async def test_initial_developer_and_review_have_independent_stage_allowances(shared, monkeypatch, limit):  # noqa: F811
    ctx = shared
    await set_attempt_limit(ctx, limit)
    async with ctx.factory() as db:
        envelope = await envelope_for(ctx, db, ctx.node)
        envelope["handoff_expect"] = {
            "execution_id": ctx.execution.id,
            "accepted_plan_version": 1,
            "claim_id": ctx.identity.claim_id,
            "claim_generation": ctx.identity.claim_generation,
        }
        await prepare_run_report(db, envelope)
        # Initial model admission verifies the immutable dispatch's human root,
        # not just the report envelope and current execution fences.
        db.add(
            OrchestrationDecision(
                org_id=ctx.node.org_id,
                flow_id=ctx.node.flow_id,
                node_id=ctx.node.id,
                kind="node_dispatched",
                actor_id="system:orchestration-dispatch",
                actor_kind="service",
                actor_role="engine",
                reason=json.dumps(
                    {"run_id": ctx.root, "attempt": ctx.node.attempts, "root_decision_id": envelope["orchestration"]["root_decision_id"]}
                ),
            )
        )
        await db.commit()
    await model_call(ctx, envelope["run_report"]["credential"], monkeypatch)
    await ctx.finish(ctx.root)
    result = await protocol.tick(ctx)
    execution, _, node, actions = await protocol.state(ctx)
    assert result.effects_succeeded == 1 and len(ctx.calls) == 1
    assert execution.attempts == 1 and node.attempts == 1
    reviewer = ctx.calls[0]
    assert reviewer["persona"] == "agent-codex-reviewer"
    # The final allowed review can still authenticate its model requests.
    await model_call(ctx, reviewer["run_report"]["credential"], monkeypatch)
    await protocol.review(ctx, findings=[{"finding_id": "F1", "summary": "Repair", "evidence_refs": []}])
    result = await protocol.tick(ctx)
    assert result.effects_succeeded == 0 and len(ctx.calls) == 1
    execution, _, _, actions = await protocol.state(ctx)
    # An explicit reviewer blocker is not a request to run the same review again.
    assert execution.status == "blocked" and execution.attempts == 1 and len(actions) == 1
    assert execution.block_detail == "reviewer_delivery_blocked"


async def test_different_story_admissions_serialize_before_worker_count(shared, monkeypatch):  # noqa: F811
    ctx = shared
    monkeypatch.setattr("src.orchestration.shared_policy._active_count", _active_count)
    async with ctx.factory() as db:
        nodes = []
        for number in (44, 45):
            node = OrchestrationNode(
                org_id=protocol.ORG,
                flow_id=ctx.flow.id,
                epic_ref="E1",
                wave_ref="W1",
                node_ref=f"N{number}",
                kind="story",
                state="ready",
                title=f"Story {number}",
                issue_ref=str(number),
                attempts=0,
            )
            db.add(node)
            await db.flush()
            db.add(
                OrchestrationWorkClaim(
                    id=f"claim-{number}",
                    org_id=protocol.ORG,
                    provider_repository_id=123,
                    issue_number=number,
                    owner_kind="engine_flow",
                    owner_ref=ctx.flow.id,
                    state="held",
                    generation=1,
                    active_run_id=attempt_run_id(node.id, 1),
                    claim_event_id=f"claim-{number}",
                )
            )
            nodes.append(node)
        await db.commit()

    async def admit(db, node):
        return await authorize_shared_dispatch(
            db,
            node=node,
            principal_user_id="human",
            target_repository=protocol.REPO,
            provider_repository_id=123,
            expected_invocation_id=attempt_run_id(node.id, 1),
        )

    started = asyncio.Event()

    async def second_admission():
        async with ctx.factory() as db:
            node = await db.get(OrchestrationNode, nodes[1].id)
            started.set()
            result = await admit(db, node)
            await db.commit()
            return result

    pending = None
    try:
        async with ctx.factory() as db:
            first = await db.get(OrchestrationNode, nodes[0].id)
            assert (await admit(db, first)).permitted
            pending = asyncio.create_task(second_admission())
            await asyncio.wait_for(started.wait(), timeout=5)
            # PostgreSQL must keep the second admission behind the first commit.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), timeout=0.2)
            first.state, first.attempts = "running", 1
            envelope = await envelope_for(ctx, db, first)
            await prepare_run_report(db, envelope)
            await db.commit()
        denied = await asyncio.wait_for(pending, timeout=5)
        assert not denied.permitted and denied.reason.value == "concurrency_limit_exceeded"
        async with ctx.factory() as db:
            assert await db.get(OrchestrationRunReport, envelope["message_id"]) is not None
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize(
    "used,approved,allowed",
    [(2, False, True), (2, True, True), (3, False, True), (8, False, False), (3, True, True), (18, True, True), (19, True, True), (20, True, False)],
)
async def test_accepted_review_allowance_controls_retries_and_preserves_attempts(shared, used, approved, allowed):  # noqa: F811
    from src.orchestration.compile import ApprovalContext
    from src.orchestration.continuation import digest
    from src.orchestration.execution_policy import stamp_policy
    from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
    from src.orchestration.models import OrchestrationExecution
    from src.orchestration.repository import OrchestrationRepository
    from src.orchestration.review_cycle import PHASES, ReviewCycleHandler
    from src.orchestration.shared_retry import RetryIncreaseRequest, accept_retry_increase, preview_retry_increase

    ctx = shared
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        draft = ExecutionPolicy.model_validate(ctx.policy.model_dump(mode="json", exclude={"policy_id", "policy_hash", "principal_id"}))
        ctx.policy = stamp_policy(draft, principal_id="human", org_id=ctx.node.org_id)
        plan.plan_document = {**plan.plan_document, "execution_policy": ctx.policy.model_dump(mode="json")}
        plan.plan_hash = digest(plan.plan_document)
        execution = await db.get(OrchestrationExecution, ctx.execution.id)
        execution.attempts = used
        from src.orchestration.models import OrchestrationAction

        for index in range(used):
            db.add(
                OrchestrationAction(
                    org_id=execution.org_id,
                    execution_id=execution.id,
                    operation_key=f"historical-review:{index}",
                    kind="historical_effect",
                    status="failed",
                    attempt=index + 1,
                    detail={"attempt_stage": "review"},
                    created_at=datetime.now(UTC),
                )
            )
        execution.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.flush()
        if approved:
            actor = ApprovalContext(org_id=ctx.node.org_id, actor_id="human", actor_role="platform_admin")
            request = RetryIncreaseRequest(
                expected_plan_version=plan.version,
                expected_plan_hash=plan.plan_hash,
                max_attempts_per_node=20,
                reason="Owner authorizes twenty attempts for this flow.",
            )
            result = await preview_retry_increase(db, flow_id=ctx.flow.id, actor=actor, request=request)
            await accept_retry_increase(
                db, flow_id=ctx.flow.id, actor=actor, request=request.model_copy(update={"expected_snapshot": result["snapshot"]})
            )
        await db.commit()
    handler = ReviewCycleHandler(ctx.factory, ctx.service)
    notifier = AsyncMock(return_value="notice")
    result = await run_execution_runner(
        ctx.factory,
        handlers=dict.fromkeys(PHASES, handler),
        config=RunnerConfig(enabled=True, max_attempts=3, io_timeout_seconds=10),
        notifier=notifier,
    )
    assert not result.errors
    assert result.effects_succeeded == int(allowed)
    execution, _, node, actions = await protocol.state(ctx)
    assert node.attempts == 1 and execution.attempts == used + int(allowed)
    assert len(ctx.calls) == len(actions) == int(allowed)
    if not allowed:
        assert execution.status == "blocked"
        assert execution.block_code == "attempts_exhausted"
        # Later scheduler ticks must neither reset spent attempts nor dispatch
        # another worker once the accepted allowance has been exhausted.
        for _ in range(3):
            async with ctx.factory() as db:
                states = await OrchestrationRepository(db).node_display_states(org_id=node.org_id, flow_id=node.flow_id)
                assert states[node.id] == "stalled"
                row = await db.get(OrchestrationExecution, execution.id)
                row.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
                await db.commit()
            result = await run_execution_runner(
                ctx.factory,
                handlers=dict.fromkeys(PHASES, handler),
                config=RunnerConfig(enabled=True, max_attempts=3, io_timeout_seconds=10),
                notifier=notifier,
            )
            assert not result.errors and result.blocked == 1
            assert result.reserved == result.effects_attempted == 0
            execution, _, node, actions = await protocol.state(ctx)
            assert execution.status == "blocked" and execution.block_code == "attempts_exhausted"
            assert execution.attempts == used and node.attempts == 1
            assert not ctx.calls and not actions
        notifier.assert_awaited_once()


@pytest.mark.parametrize("used", [3, 20])
async def test_exhausted_story_can_still_record_reviewer_merge(shared, monkeypatch, used):  # noqa: F811
    from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
    from src.orchestration.merge_controller import PHASES, MergeController
    from src.orchestration.models import OrchestrationExecution
    from tests.orchestration import test_merge_controller as merge_protocol

    async with merge_protocol.prepared_merge(shared, monkeypatch) as ctx:
        async with ctx.factory() as db:
            execution = await db.get(OrchestrationExecution, ctx.execution.id)
            execution.attempts = used
            execution.status = "blocked"
            execution.block_code = "attempts_exhausted"
            execution.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        ctx.merge_remote()
        result = await run_execution_runner(
            ctx.factory,
            handlers=dict.fromkeys(PHASES, MergeController(ctx.factory, ctx.merge_services)),
            config=RunnerConfig(enabled=True, max_attempts=3, io_timeout_seconds=10),
            notifier=AsyncMock(return_value="notice"),
        )
        assert not result.errors and result.advanced == 1
        assert result.reserved == result.effects_attempted == result.blocked == 0
        execution, _, node, _ = await protocol.state(ctx)
        assert node.state == "passed"
        assert execution.phase == "deployment_pending" and execution.status == "runnable"
        assert execution.attempts == used and execution.block_code is None
        assert ctx.mutations == [] and len(ctx.calls) == 1
