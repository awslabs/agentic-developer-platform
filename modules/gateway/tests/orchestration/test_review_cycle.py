"""Real SQL and protected dispatch: one PR/claim/allowance through fresh review."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agentauth.engine import EngineAuthorityWriter
from src.budget.enforcement_settings import BudgetAccountingGap, BudgetEnforcementSetting
from src.orchestration.dispatch_pass import DispatchPassConfig, PendingPublish, _build_envelope, attempt_run_id
from src.orchestration.execution_policy import Action, AuthorizationContext, Decision, ExecutionPolicy, PolicyLimits
from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
from src.orchestration.execution_state import ExecutionIdentity, PhaseAdvance
from src.orchestration.execution_store import advance_execution, create_execution, load_execution
from src.orchestration.genesis import resolve_engine_genesis
from src.orchestration.handoff import commit_handoff
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.pr_bindings import PullRequestIdentity
from src.orchestration.review_cycle import PHASES, ReviewCycleHandler
from src.orchestration.review_cycle_dispatch import ReviewCycleServices, continuation_run_id, current_author_run, validate_continuation_assignment
from src.orchestration.run_reports import OrchestrationRunReport
from src.shared.models.base import Base
from tests.agentauth.test_human_dispatch import store  # noqa: F401
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

HEAD = "a" * 40
ORG = "cycle-org"
REPO = "org/repo"


@pytest.fixture
async def cycle(pg_url, store, monkeypatch, request):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    models = [
        BudgetEnforcementSetting,
        BudgetAccountingGap,
        OrchestrationFlow,
        OrchestrationAcceptedPlan,
        OrchestrationNode,
        OrchestrationDecision,
        OrchestrationWorkClaim,
        OrchestrationExecution,
        OrchestrationAction,
        OrchestrationPullRequestBinding,
        OrchestrationRunReport,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[model.__table__ for model in models]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    delivery = getattr(request, "param", {}).get("delivery", False)
    policy = ExecutionPolicy(
        schema_version=2 if delivery else 1,
        user_credentials={
            "permission_mode": "user_configured",
            "lifetime": "provider_managed",
            "vault_credential_ids": ["delivery-connection"],
            "aws_role_arns": ["arn:aws:iam::123456789012:role/test"],
            "actions": [Action.DEPLOY],
        }
        if delivery
        else None,
        org_id=ORG,
        principal_id="human",
        policy_id="policy",
        policy_hash="b" * 64,
        repository_ids=[REPO],
        allowed_actions=[Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.MERGE] + ([Action.DEPLOY] if delivery else []),
        environment_connection_ids=["delivery-connection"] if delivery else [],
        expires_at=now + timedelta(days=1),
        limits=PolicyLimits(max_wall_clock_seconds=3600, max_spend_usd=Decimal(25), max_attempts_per_node=8, max_concurrent_actions=1),
    )
    async with factory() as db:
        flow = OrchestrationFlow(execution_paused=False, org_id=ORG, slug="cycle", title="Cycle", state="running")
        db.add(flow)
        await db.flush()
        approval = OrchestrationDecision(org_id=ORG, flow_id=flow.id, kind="plan_accepted", actor_kind="human", actor_id="human", actor_role="owner")
        node = OrchestrationNode(
            org_id=ORG,
            flow_id=flow.id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="N1",
            kind="story",
            state="running",
            title="Bound story",
            issue_ref="43",
            attempts=1,
        )
        db.add_all([approval, node])
        await db.flush()
        root = attempt_run_id(node.id, 1)
        plan = OrchestrationAcceptedPlan(
            org_id=ORG, flow_id=flow.id, version=1, plan_hash="b" * 64, plan_document={"execution_policy": policy.model_dump(mode="json")}
        )
        claim = OrchestrationWorkClaim(
            id="cycle-claim",
            org_id=ORG,
            provider_repository_id=123,
            issue_number=43,
            owner_kind="engine_flow",
            owner_ref=flow.id,
            state="held",
            generation=5,
            active_run_id=root,
            claim_event_id=root,
        )
        binding = OrchestrationPullRequestBinding(
            org_id=ORG,
            flow_id=flow.id,
            node_id=node.id,
            attempt=1,
            run_id=root,
            provider_repository_id=123,
            provider_pr_node_id="PR_cycle",
            repo=REPO,
            pr_number=77,
            installation_id=42,
            head_sha=HEAD,
            revision=1,
            role="implementation",
            state="active",
            registered_by=root,
            registered_by_kind="service",
            accepted_scope=json.dumps({"node": {"kind": "story", "issue_ref": "43", "title": "Bound story"}}),
        )
        db.add_all([plan, claim, binding])
        await db.flush()
        identity = ExecutionIdentity(ORG, node.id, 1, 1, claim.id, 5)
        created = await create_execution(db, identity=identity, flow_id=flow.id, next_check_at=now)
        await commit_handoff(db, identity=identity, now=now, next_check_at=now)
        genesis = await resolve_engine_genesis(db, org_id=ORG, decision_id=approval.id)
        await db.commit()
    writer = EngineAuthorityWriter(store=store, events_table="cycle-events")
    store.client.create_table(
        TableName="cycle-events",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
    )
    config = DispatchPassConfig(queue_url="https://sqs.test/cycle.fifo", repo=REPO)
    envelope = _build_envelope(
        node=node, genesis=genesis, graph_address="cycle/E1/W1/N1", installation_id=42, issue=43, config=config, user_id="human", cognito_sub="sub"
    )
    envelope["source_ref"]["provider_repository_id"] = 123
    envelope["handoff_required"] = True
    writer.provision(
        PendingPublish(node_id=node.id, org_id=ORG, envelope=envelope, group_id="cycle", deduplication_id=root, genesis=genesis, node_attempt=1)
    )
    calls = []
    queue = SimpleNamespace(send_message=lambda **kwargs: calls.append(json.loads(kwargs["MessageBody"])) or {"MessageId": "sent"})
    service = ReviewCycleServices(factory, writer=writer, queue=queue, config=config)
    ctx = SimpleNamespace(
        factory=factory,
        identity=identity,
        execution=created.record,
        flow=flow,
        node=node,
        plan=plan,
        approval=approval,
        claim=claim,
        binding=binding,
        root=root,
        store=store,
        service=service,
        calls=calls,
        head=HEAD,
        policy=policy,
        spend=Decimal(2),
    )
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.resolve_root_user_entity_id", AsyncMock(return_value="human"))
    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.resolve_user_entity_id", AsyncMock(return_value="sub"))
    ctx.launch = AsyncMock(return_value={"model_resolved": "openai.gpt-6-sol"})
    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.resolve_launch_configuration", ctx.launch)
    monkeypatch.setattr("src.orchestration.runtime_policy.flow_started_at", AsyncMock(return_value=now))

    async def meter(**kwargs):
        return SimpleNamespace(total_usd=ctx.spend)

    monkeypatch.setattr("src.orchestration.flow_meter.read_flow_meter", meter)

    async def authorization(db, **kwargs):
        return AuthorizationContext(
            policy=kwargs["policy"],
            accepted_plan_version=1,
            in_force_plan_version=1,
            principal_id="human",
            member_org_id=ORG,
            principal_can_authorize=True,
            now=datetime.now(UTC),
            credential_scope=kwargs["credential_scope"],
            observed_spend_usd=ctx.spend,
            observed_attempts=ctx.node.attempts,
            observed_concurrency=1,
            work_owned_by_policy_flow=True,
        )

    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.resolve_authorization_context", authorization)
    # Redis reservation service is external; its refusal is exercised below. SQL,
    # live-grant lineage, provider fences and authorize_action remain production.
    reserve = AsyncMock(return_value=Decision.permit("same flow reservation"))
    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.authorize_node_dispatch", reserve)
    ctx.reserve = reserve

    async def provider(**kwargs):
        return PullRequestIdentity(123, "PR_cycle", REPO, 77, ctx.head)

    monkeypatch.setattr("src.orchestration.pr_identity.resolve_pr_identity", provider)

    async def finish(run):
        item = store._read(f"TENANT#{ORG}", f"EXEC#{run}")
        item.update(status={"S": "completed"}, terminal_outcome={"S": "complete"})
        store.client.put_item(TableName=store.table, Item=item)

    ctx.finish = finish
    await finish(root)
    yield ctx
    await engine.dispose()


async def tick(ctx, *, checkpoint=None):
    async with ctx.factory() as db:
        loaded = await load_execution(db, identity=ctx.identity)
        loaded_record = loaded.record
        await advance_execution(
            db,
            identity=ctx.identity,
            advance=PhaseAdvance(
                phase=loaded_record.phase, status=loaded_record.status, expected_revision=loaded_record.revision, next_check_at=datetime.now(UTC)
            ),
        )
        await db.commit()
    handler = ReviewCycleHandler(ctx.factory, ctx.service)
    return await run_execution_runner(
        ctx.factory,
        handlers=dict.fromkeys(PHASES, handler),
        config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
        notifier=AsyncMock(return_value="test-notice"),
        checkpoint=checkpoint,
    )


async def state(ctx):
    async with ctx.factory() as db:
        execution = await db.get(OrchestrationExecution, ctx.execution.id)
        claim = await db.get(OrchestrationWorkClaim, ctx.identity.claim_id)
        node = await db.get(OrchestrationNode, ctx.node.id)
        actions = list(
            (
                await db.scalars(
                    select(OrchestrationAction)
                    .where(OrchestrationAction.kind == "review_cycle_dispatch")
                    .order_by(OrchestrationAction.created_at, OrchestrationAction.id)
                )
            ).all()
        )
        return execution, claim, node, actions


async def review(ctx, *, approve=False, findings=None, publication=False):
    execution, claim, _, actions = await state(ctx)
    async with ctx.factory() as db:
        db.add(
            OrchestrationAction(
                org_id=ORG,
                execution_id=execution.id,
                operation_key=f"evidence:{claim.active_run_id}:{ctx.head}",
                kind="review_evidence",
                status="succeeded",
                artifact_ref="artifact:review",
                receipt_ref="artifact:review",
                detail={
                    "reviewed_head_sha": ctx.head,
                    "complete_review": "true" if approve else "false",
                    "publication_outstanding": "true" if publication else "false",
                    "cycle_input": json.dumps(
                        {
                            "reviewer_run_id": claim.active_run_id,
                            "author_run_id": actions[-1].detail["author_run_id"],
                            "observed_at": datetime.now(UTC).isoformat(),
                            "findings": findings or [],
                        }
                    ),
                },
            )
        )
        await db.commit()
    await ctx.finish(claim.active_run_id)


async def test_develop_review_repair_fresh_review_merge_ready(cycle):
    ctx = cycle
    from src.agentauth.model_policy import _persist_snapshot
    from tests.agentauth.test_model_policy import live_snapshot

    parent_snapshot = live_snapshot(tenant_id=ORG, correlation_id=ctx.flow.id, root_invocation_id=ctx.root)
    # The developer has already completed; seed the immutable snapshot it would
    # have received during its normal pending admission.
    parent = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{ctx.root}")
    parent["status"] = {"S": "pending"}
    ctx.store.client.put_item(TableName=ctx.store.table, Item=parent)
    expected_digest = await _persist_snapshot(store=ctx.store, invocation_id=ctx.root, tenant_id=ORG, snapshot=parent_snapshot)
    await ctx.finish(ctx.root)
    original_send = ctx.service.queue.send_message

    def assert_snapshot_before_send(**kwargs):
        envelope = json.loads(kwargs["MessageBody"])
        child = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{envelope['message_id']}")
        assert child["status"] == {"S": "pending"}
        assert child["model_policy_snapshot_digest"] == {"S": expected_digest}
        assert child["model_policy_root_invocation_id"] == {"S": ctx.root}
        assert child["model_policy_correlation_id"] == {"S": ctx.flow.id}
        return original_send(**kwargs)

    ctx.service.queue.send_message = assert_snapshot_before_send
    result = await tick(ctx)
    assert result.effects_succeeded == 1, (result, (await state(ctx))[0].block_detail)
    first = ctx.calls[-1]
    assert first["persona"] == "agent-codex-reviewer"
    assert first["model_resolved"] == "openai.gpt-6-sol"
    assert ctx.launch.await_args.kwargs == {"org_id": ORG, "user_id": "human", "persona": "agent-codex-reviewer"}
    assert first["review_expect"]["author_run_id"] == ctx.root
    assert first["review_cycle_input"]["allow_story_repairs"] is True
    assert first["review_expect"]["allow_story_repairs"] is True
    protected = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{first['message_id']}")
    assert protected["orchestration_review_repairs"] == {"BOOL": True}
    await review(ctx, findings=[{"finding_id": "F1", "summary": "Repair the failing boundary", "evidence_refs": []}])
    result = await tick(ctx)
    assert result.effects_succeeded == 1, result
    repair = ctx.calls[-1]
    assert repair["persona"] == "agent-codex-reviewer"
    assert repair["review_cycle_input"]["findings"][0]["finding_id"] == "F1"
    assert repair["review_cycle_input"]["head_sha"] == HEAD
    assert repair["review_cycle_input"]["pr_number"] == 77
    assert repair["correlation"]["chain_depth"] == 2
    assert repair["correlation"]["correlation_id"] == ctx.root
    ctx.head = "b" * 40
    await ctx.finish(repair["message_id"])
    result = await tick(ctx)
    assert result.effects_succeeded == 1, result
    fresh = ctx.calls[-1]
    assert fresh["review_expect"]["expected_head_sha"] == ctx.head
    assert fresh["review_expect"]["author_run_id"] == repair["message_id"]
    assert fresh["message_id"] != repair["message_id"]
    await review(ctx, approve=True)
    result = await tick(ctx)
    execution, claim, node, actions = await state(ctx)
    assert execution.phase == "merge_ready", result
    assert execution.status == "runnable"
    assert node.state == "running" and node.attempts == 1
    assert claim.generation == 5 and claim.state == "held"
    assert len(actions) == 3 and execution.attempts == 3
    assert len(ctx.calls) == 3
    assert all(call["model_resolved"] == "openai.gpt-6-sol" for call in ctx.calls)
    async with ctx.factory() as db:
        assert await current_author_run(db, node=node, default=ctx.root) == repair["message_id"]
        binding = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
        assert binding.head_sha == ctx.head and binding.revision == 2
        assert binding.run_id == ctx.root and binding.accepted_scope == ctx.binding.accepted_scope


async def test_review_repairs_current_head_and_advances_without_another_reviewer(cycle):
    ctx = cycle
    result = await tick(ctx)
    assert result.effects_succeeded == 1
    assigned = ctx.calls[-1]
    assert assigned["review_expect"]["allow_story_repairs"] is True
    ctx.head = "b" * 40
    await review(ctx, approve=True)
    result = await tick(ctx)
    execution, _, node, _ = await state(ctx)
    assert execution.phase == "merge_ready", result
    assert len(ctx.calls) == 1
    async with ctx.factory() as db:
        binding = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
        assert binding.head_sha == ctx.head
        assert await current_author_run(db, node=node, default=ctx.root) == ctx.root


async def test_review_only_policy_does_not_gain_branch_write_access(cycle):
    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["allowed_actions"].remove("repair")
        plan.plan_document = document
        await db.commit()
    assert (await tick(cycle)).effects_succeeded == 1
    assigned = cycle.calls[-1]
    assert assigned["review_cycle_input"]["allow_story_repairs"] is False
    assert assigned["review_expect"]["allow_story_repairs"] is False
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{assigned['message_id']}")
    assert "orchestration_review_repairs" not in raw


@pytest.mark.parametrize("gate", ["failed", "halted", "rejected", "awaiting_gate"])
async def test_outer_gates_never_resurrect(cycle, gate):
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        node.state = gate
        await db.commit()
    await tick(cycle)
    assert not cycle.calls
    _, _, node, _ = await state(cycle)
    assert node.state == gate


@pytest.mark.parametrize("fault", ["no_pr", "scope", "revoked", "budget", "claim", "unknown_review", "publication"])
async def test_typed_blocks_without_new_identity_or_allowance(cycle, fault):
    ctx = cycle
    if fault in {"unknown_review", "publication"}:
        assert (await tick(ctx)).effects_succeeded == 1
        await review(ctx, publication=fault == "publication")
    elif fault == "revoked":
        authority = ctx.store._read(f"TENANT#{ORG}", f"AUTHORITY#{ctx.approval.id}")
        authority["status"] = {"S": "revoked"}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=authority)
    elif fault == "budget":
        ctx.spend = Decimal(25)
    else:
        async with ctx.factory() as db:
            if fault in {"no_pr", "scope"}:
                binding = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
                if fault == "no_pr":
                    binding.state = "superseded"
                else:
                    binding.accepted_scope = '{"node":{}}'
            else:
                claim = await db.get(OrchestrationWorkClaim, ctx.claim.id)
                claim.active_run_id = "other"
            await db.commit()
    before = len(ctx.calls)
    result = await tick(ctx)
    execution, claim, node, _ = await state(ctx)
    assert result.blocked == 1, result
    assert execution.block_code and execution.block_required_input
    assert len(ctx.calls) == before and claim.generation == 5 and node.attempts == 1


async def test_changed_head_requires_fresh_review(cycle):
    assert (await tick(cycle)).effects_succeeded == 1
    await review(cycle, approve=True)
    cycle.head = "b" * 40
    result = await tick(cycle)
    assert result.effects_succeeded == 1, result
    assert len(cycle.calls) == 2
    assert cycle.calls[-1]["review_expect"]["expected_head_sha"] == cycle.head
    assert (await state(cycle))[0].phase != "merge_ready"


async def test_recovery_after_intent_keeps_action_and_run_identity(cycle):
    async def crash(name, context):
        if name == "after_intent":
            raise RuntimeError("process exited")

    assert (await tick(cycle, checkpoint=crash)).errors == 1
    before = (await state(cycle))[3]
    assert len(before) == 1 and not cycle.calls
    result = await tick(cycle)
    assert result.effects_succeeded == 1, result
    after = (await state(cycle))[3]
    assert len(after) == 1 and after[0].operation_key == before[0].operation_key
    assert cycle.calls[-1]["message_id"] == continuation_run_id(before[0].operation_key)


async def test_recovery_after_queue_ack_reuses_protected_execution(cycle):
    async def crash(name, context):
        if name == "after_effect":
            raise RuntimeError("process exited")

    assert (await tick(cycle, checkpoint=crash)).errors == 1
    assert len(cycle.calls) == 1
    result = await tick(cycle)
    assert result.effects_succeeded == 1, result
    assert cycle.calls[0] == cycle.calls[1]
    execution, claim, _, actions = await state(cycle)
    assert len(actions) == 1 and claim.generation == 5 and claim.active_run_id == cycle.calls[0]["message_id"]
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{claim.active_run_id}")
    grant = cycle.store.live_grant(invocation_id=claim.active_run_id, tenant_id=ORG, attempt=1, now=datetime.now(UTC))
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        await validate_continuation_assignment(db, execution=raw, grant=grant, node=node)


async def test_revocation_after_intent_is_a_typed_block_without_publication(cycle):
    async def revoke(name, context):
        if name == "after_intent":
            authority = cycle.store._read(f"TENANT#{ORG}", f"AUTHORITY#{cycle.approval.id}")
            authority["status"] = {"S": "revoked"}
            cycle.store.client.put_item(TableName=cycle.store.table, Item=authority)

    result = await tick(cycle, checkpoint=revoke)
    assert result.effects_uncertain == 1
    result = await tick(cycle)
    assert result.blocked == 1
    assert not cycle.calls
    execution, claim, node, actions = await state(cycle)
    assert execution.block_code == "authority_unverifiable"
    assert claim.active_run_id == cycle.root and claim.generation == 5
    assert len(actions) == 1 and node.attempts == 1


async def test_timed_out_send_republishes_same_envelope_without_duplicate_admission(cycle):
    queue = cycle.service.queue
    original = queue.send_message

    def lost_response(**kwargs):
        original(**kwargs)
        raise TimeoutError("response lost")

    queue.send_message = lost_response
    result = await tick(cycle)
    assert result.effects_uncertain == 1
    queue.send_message = original
    result = await tick(cycle)
    assert result.effects_succeeded == 1, result
    assert cycle.calls[0] == cycle.calls[1]
    assert len((await state(cycle))[3]) == 1


async def test_same_generation_cannot_overlap_running_mutator(cycle):
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}")
    raw["status"] = {"S": "running"}
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    await tick(cycle)
    assert not cycle.calls
    assert (await state(cycle))[1].active_run_id == cycle.root


async def test_bootstrap_admission_preserves_transferred_claim_generation(cycle, monkeypatch):
    from src.orchestration.work_admission import admit_pending

    monkeypatch.setattr("src.agentauth.model_policy.ensure_snapshot_report_only", AsyncMock(return_value={}))
    assert (await tick(cycle)).effects_succeeded == 1
    run = cycle.calls[-1]["message_id"]
    async with cycle.factory() as db:
        receipt = await admit_pending(cycle.store, run, session=db)
        await db.commit()
    assert receipt["claim_id"] == cycle.identity.claim_id
    assert receipt["generation"] == cycle.identity.claim_generation
    assert (await state(cycle))[1].active_run_id == run


async def test_registered_handler_observes_failure_before_handoff(cycle):
    async with cycle.factory() as db:
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        execution.phase = "admitted"
        binding = await db.get(OrchestrationPullRequestBinding, cycle.binding.id)
        binding.state = "superseded"
        await db.commit()
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}")
    raw["terminal_outcome"] = {"S": "failed"}
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    result = await tick(cycle)
    assert result.blocked == 1, result
    assert (await state(cycle))[0].block_detail == "worker_failed_or_halted"
    assert not cycle.calls


async def test_concurrent_ticks_admit_one_successor(cycle):
    import asyncio

    handler = ReviewCycleHandler(cycle.factory, cycle.service)
    kwargs = dict(
        handlers=dict.fromkeys(PHASES, handler),
        config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
        notifier=AsyncMock(return_value="notice"),
    )
    reports = await asyncio.gather(run_execution_runner(cycle.factory, **kwargs), run_execution_runner(cycle.factory, **kwargs))
    execution, claim, node, actions = await state(cycle)
    assert not any(report.errors for report in reports)
    assert len(actions) == 1 and len({message["message_id"] for message in cycle.calls}) == 1
    assert claim.generation == 5 and node.attempts == 1
    assert claim.active_run_id == continuation_run_id(actions[0].operation_key)


async def test_policy_developer_cannot_dispatch_competing_review(cycle):
    from src.agentauth.grants import AgentAction

    grant = cycle.store.live_grant(invocation_id=cycle.root, tenant_id=ORG, attempt=1, now=datetime.now(UTC))
    assert grant.allowed_actions == frozenset({AgentAction.MONITOR})
    assert AgentAction.MONITOR in grant.delegable_actions


async def test_repair_has_separate_allowance_and_review_retains_its_history(cycle):
    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = 3
        plan.plan_document = document
        await db.commit()
    assert (await tick(cycle)).effects_succeeded == 1
    await review(cycle, findings=[{"finding_id": "F1", "summary": "Correction required"}])
    assert (await tick(cycle)).effects_succeeded == 1
    await cycle.finish(cycle.calls[-1]["message_id"])
    cycle.head = "b" * 40
    result = await tick(cycle)
    assert result.effects_succeeded == 1, result
    execution, claim, node, actions = await state(cycle)
    from src.orchestration.stage_attempts import stage_attempts

    async with cycle.factory() as db:
        assert await stage_attempts(db, org_id=ORG, node_id=node.id, action=Action.REVIEW) == 2
        assert await stage_attempts(db, org_id=ORG, node_id=node.id, action=Action.REPAIR) == 1
    assert len(cycle.calls) == 3 and len(actions) == 3
    assert claim.generation == 5 and node.attempts == 1


async def test_result_adapter_cannot_pass_policy_story_from_worker_or_merge(cycle, monkeypatch):
    from src.orchestration.results import observe_results

    async with cycle.factory() as db:
        db.add(
            OrchestrationDecision(
                org_id=ORG,
                flow_id=cycle.flow.id,
                node_id=cycle.node.id,
                kind="node_dispatched",
                actor_id="system:orchestration-dispatch",
                actor_kind="service",
                actor_role="engine",
                reason=json.dumps({"attempt": 1, "run_id": cycle.root, "arrived_at": "2026-09-20T00:00:00Z"}),
            )
        )
        await db.commit()
    run_store = SimpleNamespace(get=lambda *args: {"tenant_id": ORG, "engine_node_id": cycle.node.id, "engine_attempt": 1, "status": "complete"})
    merged = AsyncMock(return_value=("https://github.com/org/repo/pull/77", ""))
    monkeypatch.setattr("src.orchestration.results._story_evidence", merged)
    async with cycle.factory() as db:
        result = await observe_results(db, run_store=run_store)
        await db.commit()
    assert result.waiting == 1 and result.advanced == 0
    assert not merged.called
    assert (await state(cycle))[2].state == "running"


async def test_paused_reviewer_waits_and_resume_dispatches_once(cycle):
    async with cycle.factory() as db:
        flow = await db.get(OrchestrationFlow, cycle.node.flow_id)
        flow.execution_paused = True
        await db.commit()
    for _ in range(2):
        report = await tick(cycle)
        assert report.errors == 0 and report.effects_attempted == 0
    execution, _, _, actions = await state(cycle)
    assert execution.attempts == 0 and actions == []
    assert cycle.calls == []
    async with cycle.factory() as db:
        flow = await db.get(OrchestrationFlow, cycle.node.flow_id)
        flow.execution_paused = False
        await db.commit()
    report = await tick(cycle)
    assert report.effects_succeeded == 1
    assert len(cycle.calls) == 1


async def test_final_developer_attempt_still_admits_first_review(cycle):
    # Development has spent its only attempt; review has spent none.
    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = 1
        plan.plan_document = document
        await db.commit()
    result = await tick(cycle)
    assert result.effects_succeeded == 1, result
    execution, _, node, actions = await state(cycle)
    assert node.attempts == 1 and execution.attempts == 1
    assert len(actions) == 1 and actions[0].detail["action"] == "review"


@pytest.mark.parametrize("allowance", [1, 2])
async def test_missing_snapshot_startup_failure_retries_without_resetting_history(cycle, allowance):
    from src.agentauth.bootstrap_failure import is_bootstrap_failure, record_refusal
    from src.agentauth.model_policy import _persist_snapshot
    from tests.agentauth.test_model_policy import live_snapshot

    ctx = cycle
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = allowance
        plan.plan_document = document
        await db.commit()
    parent = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{ctx.root}")
    parent["status"] = {"S": "pending"}
    ctx.store.client.put_item(TableName=ctx.store.table, Item=parent)
    digest = await _persist_snapshot(
        store=ctx.store,
        invocation_id=ctx.root,
        tenant_id=ORG,
        snapshot=live_snapshot(tenant_id=ORG, correlation_id=ctx.flow.id, root_invocation_id=ctx.root),
    )
    await ctx.finish(ctx.root)
    assert (await tick(ctx)).effects_succeeded == 1
    failed_run = ctx.calls[-1]["message_id"]
    raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{failed_run}")
    # Reproduce the already-deployed producer's missing snapshot and initial pod
    # bind. No credential or control registration was delivered to this worker.
    raw.pop("model_policy_snapshot")
    raw.pop("model_policy_snapshot_digest")
    raw.update(status={"S": "active"}, workload_binding={"S": "failed-bootstrap-pod"})
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    record = ctx.store.authority.load_execution(invocation_id=failed_run, tenant_id=ORG)
    record_refusal(ctx.store, record=record, request_id="gateway-refusal-request", events_table="cycle-events")
    record_refusal(ctx.store, record=record, request_id="gateway-refusal-request", events_table="cycle-events")
    failed = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{failed_run}")
    assert is_bootstrap_failure(failed)
    assert failed["terminal_outcome"] == {"S": "failed"}
    result = await tick(ctx)
    if allowance == 1:
        execution, claim, node, actions = await state(ctx)
        assert result.effects_succeeded == 0 and len(ctx.calls) == 1
        assert len(actions) == 1 and node.attempts == 1
        assert claim.active_run_id == failed_run and claim.generation == 5
        assert execution.block_code == "attempts_exhausted"
        return
    assert result.effects_succeeded == 1, (result, (await state(ctx))[0].block_detail)
    retry = ctx.calls[-1]
    assert retry["message_id"] != failed_run
    assert retry["review_expect"]["author_run_id"] == ctx.root
    child = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{retry['message_id']}")
    assert child["model_policy_snapshot_digest"] == {"S": digest}
    execution, claim, node, actions = await state(ctx)
    assert node.attempts == 1 and claim.generation == 5
    assert len(actions) == 2 and execution.attempts == 2
    assert actions[-1].detail["bootstrap_retry_of"] == failed_run
    assert claim.active_run_id == retry["message_id"]
    await review(ctx, approve=True)
    await tick(ctx)
    assert (await state(ctx))[0].phase == "merge_ready"


async def test_issued_bootstrap_authority_cannot_be_reclassified_as_startup_failure(cycle):
    from src.agentauth.bootstrap_failure import record_issuance, record_refusal
    from src.agentauth.store import AuthorityStoreError

    ctx = cycle
    assert (await tick(ctx)).effects_succeeded == 1
    run = ctx.calls[-1]["message_id"]
    raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{run}")
    raw.update(status={"S": "active"}, workload_binding={"S": "live-worker"})
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    record = ctx.store.authority.load_execution(invocation_id=run, tenant_id=ORG)
    record_issuance(ctx.store, record=record)
    with pytest.raises(AuthorityStoreError):
        record_refusal(ctx.store, record=record, request_id="late-failure", events_table="cycle-events")
    assert ctx.store._read(f"TENANT#{ORG}", f"EXEC#{run}")["status"] == {"S": "active"}
