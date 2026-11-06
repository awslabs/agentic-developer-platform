"""Real SQL and protected dispatch: one PR/claim/allowance through fresh review."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
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
from tests.orchestration.decision_projection_fixture import apply_migration

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
    async with engine.connect() as connection:
        await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[model.__table__ for model in models]))
        await connection.commit()
        await connection.run_sync(apply_migration)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    delivery = getattr(request, "param", {}).get("delivery", False)
    plan_version = getattr(request, "param", {}).get("plan_version", 1)
    claim_generation = getattr(request, "param", {}).get("claim_generation", 5)
    node_attempt = getattr(request, "param", {}).get("node_attempt", 1)
    story_repo = getattr(request, "param", {}).get("repo", REPO)
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
        repository_ids=[story_repo],
        allowed_actions=[Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.MERGE] + ([Action.DEPLOY] if delivery else []),
        environment_connection_ids=["delivery-connection"] if delivery else [],
        expires_at=now + timedelta(days=1),
        limits=PolicyLimits(
            max_wall_clock_seconds=3600,
            max_spend_usd=Decimal(25),
            max_attempts_per_node=getattr(request, "param", {}).get("max_attempts", 8),
            max_concurrent_actions=1,
        ),
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
            attempts=node_attempt,
        )
        db.add_all([approval, node])
        await db.flush()
        root = attempt_run_id(node.id, node_attempt)
        plan = OrchestrationAcceptedPlan(
            org_id=ORG, flow_id=flow.id, version=plan_version, plan_hash="b" * 64, plan_document={"execution_policy": policy.model_dump(mode="json")}
        )
        claim = OrchestrationWorkClaim(
            id="cycle-claim",
            org_id=ORG,
            provider_repository_id=123,
            issue_number=43,
            owner_kind="engine_flow",
            owner_ref=flow.id,
            state="held",
            generation=claim_generation,
            active_run_id=root,
            claim_event_id=root,
        )
        binding = OrchestrationPullRequestBinding(
            org_id=ORG,
            flow_id=flow.id,
            node_id=node.id,
            attempt=node_attempt,
            run_id=root,
            provider_repository_id=123,
            provider_pr_node_id="PR_cycle",
            repo=story_repo,
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
        identity = ExecutionIdentity(ORG, node.id, node_attempt, plan_version, claim.id, claim_generation)
        created = await create_execution(db, identity=identity, flow_id=flow.id, next_check_at=now)
        if not getattr(request, "param", {}).get("missing_handoff", False):
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
    config = DispatchPassConfig(queue_url="https://sqs.test/cycle.fifo", repo=story_repo)
    envelope = _build_envelope(
        node=node, genesis=genesis, graph_address="cycle/E1/W1/N1", installation_id=42, issue=43, config=config, user_id="human", cognito_sub="sub"
    )
    envelope["source_ref"]["provider_repository_id"] = 123
    envelope["handoff_required"] = True
    writer.provision(
        PendingPublish(
            node_id=node.id, org_id=ORG, envelope=envelope, group_id="cycle", deduplication_id=root, genesis=genesis, node_attempt=node_attempt
        )
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
    monkeypatch.setattr(
        "src.orchestration.flow_meter.reconcile_flow_meter", AsyncMock(side_effect=lambda session, **kwargs: SimpleNamespace(total_usd=ctx.spend))
    )

    async def authorization(db, **kwargs):
        return AuthorizationContext(
            policy=kwargs["policy"],
            accepted_plan_version=plan_version,
            in_force_plan_version=plan_version,
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
        return PullRequestIdentity(123, "PR_cycle", story_repo, 77, ctx.head)

    monkeypatch.setattr("src.orchestration.pr_identity.resolve_pr_identity", provider)

    async def finish(run):
        item = store._read(f"TENANT#{ORG}", f"EXEC#{run}")
        item.update(status={"S": "completed"}, terminal_outcome={"S": "complete"})
        store.client.put_item(TableName=store.table, Item=item)

    ctx.finish = finish
    await finish(root)
    yield ctx
    await engine.dispose()


async def tick(ctx, *, checkpoint=None, runner_ceiling=8, clock=None, force=True):
    if force:
        async with ctx.factory() as db:
            loaded = await load_execution(db, identity=ctx.identity)
            loaded_record = loaded.record
            await advance_execution(
                db,
                identity=ctx.identity,
                advance=PhaseAdvance(
                    phase=loaded_record.phase, status=loaded_record.status, expected_revision=loaded_record.revision, next_check_at=datetime.now(UTC)
                ),
                block=loaded_record.block,
                pending_action_key=loaded_record.pending_action_key,
            )
            await db.commit()
    handler = ReviewCycleHandler(ctx.factory, ctx.service)
    return await run_execution_runner(
        ctx.factory,
        handlers=dict.fromkeys(PHASES, handler),
        config=RunnerConfig(enabled=True, max_attempts=runner_ceiling, io_timeout_seconds=10),
        notifier=AsyncMock(return_value="test-notice"),
        checkpoint=checkpoint,
        clock=clock,
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


async def legacy_delivery(ctx):
    """Represent a retained pre-upgrade dispatch, which has no merge ownership."""
    run_id = ctx.calls[-1]["message_id"]
    ctx.calls[-1]["review_cycle_input"].pop("reviewer_owned_delivery", None)
    async with ctx.factory() as db:
        rows = (
            await db.scalars(
                select(OrchestrationDecision).where(OrchestrationDecision.node_id == ctx.node.id, OrchestrationDecision.kind == "agent_dispatched")
            )
        ).all()
        for row in rows:
            saved = json.loads(row.reason)
            if saved.get("run_id") == run_id:
                saved["envelope"]["review_cycle_input"].pop("reviewer_owned_delivery", None)
                # Seed a pre-upgrade receipt in the isolated test database.
                # Production decisions remain append-only.
                await db.execute(
                    update(OrchestrationDecision.__table__)
                    .where(OrchestrationDecision.__table__.c.id == row.id)
                    .values(reason=json.dumps(saved))
                    .execution_options(synchronize_session=False)
                )
        await db.commit()


@pytest.mark.parametrize("cycle", [{"repo": "fictional/story-b"}], indirect=True)
async def test_repair_review_keeps_story_repository_after_global_change(cycle):
    ctx = cycle
    ctx.service.config = replace(ctx.service.config, repo="fictional/platform-a")
    result = await tick(ctx)
    assert result.effects_succeeded == 1, result
    first = ctx.calls[-1]
    assert first["source_ref"]["repo"] == ctx.binding.repo
    assert first["source_ref"]["installation_id"] == ctx.binding.installation_id
    assert first["review_expect"]["flow_id"] == ctx.flow.id
    await legacy_delivery(ctx)
    await review(ctx, findings=[{"finding_id": "F1", "summary": "Repair", "evidence_refs": []}])
    result = await tick(ctx)
    assert result.effects_succeeded == 1, (result, (await state(ctx))[0].block_detail)
    repair = ctx.calls[-1]
    assert repair["source_ref"]["repo"] == ctx.binding.repo
    assert repair["source_ref"]["provider_repository_id"] == ctx.binding.provider_repository_id
    assert repair["source_ref"]["installation_id"] == ctx.binding.installation_id
    assert repair["orchestration"]["flow_id"] == ctx.flow.id


async def test_legacy_develop_review_repair_fresh_review_merge_ready(cycle):
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
    await legacy_delivery(ctx)
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
    await legacy_delivery(ctx)
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
    await legacy_delivery(ctx)
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


async def unstarted_intent(ctx):
    async def crash(name, context):
        if name == "after_intent":
            raise RuntimeError("process exited before dispatch preparation")

    assert (await tick(ctx, checkpoint=crash)).errors == 1
    return (await state(ctx))[3][-1]


async def test_changed_head_retires_only_unstarted_intent_and_preserves_attempts(cycle):
    from src.orchestration.stage_attempts import stage_attempts
    from src.orchestration.unstarted_review import retired_unstarted_dispatch

    old = await unstarted_intent(cycle)
    cycle.head = "c" * 40
    result = await tick(cycle)
    assert result.errors == 0 and not cycle.calls, result
    execution, claim, node, actions = await state(cycle)
    assert execution.pending_action_key is None
    assert retired_unstarted_dispatch(actions[0])
    assert claim.active_run_id == cycle.root and claim.generation == 5 and node.attempts == 1
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{continuation_run_id(old.operation_key)}") is None
    async with cycle.factory() as db:
        assert await stage_attempts(db, org_id=ORG, node_id=node.id, action=Action.REVIEW) == 1
    assert (await tick(cycle)).effects_succeeded == 1
    assert len(cycle.calls) == 1
    assert cycle.calls[0]["review_expect"]["expected_head_sha"] == cycle.head
    assert cycle.calls[0]["message_id"] != continuation_run_id(old.operation_key)
    execution, claim, node, actions = await state(cycle)
    assert len(actions) == 2 and retired_unstarted_dispatch(actions[0])
    assert execution.phase != "merge_ready" and node.attempts == 1


async def test_changed_head_keeps_potentially_published_intent_pending(cycle):
    async def crash(name, context):
        if name == "after_effect":
            raise RuntimeError("process exited after publication")

    assert (await tick(cycle, checkpoint=crash)).errors == 1
    old = (await state(cycle))[3][-1]
    cycle.head = "c" * 40
    result = await tick(cycle)
    assert result.blocked == 1 and len(cycle.calls) == 1
    execution, claim, _, actions = await state(cycle)
    assert execution.pending_action_key == old.operation_key
    assert actions[0].status != "failed"
    assert claim.active_run_id == continuation_run_id(old.operation_key)


async def test_unstarted_retirement_does_not_reset_stage_attempt_ceiling(cycle):
    await unstarted_intent(cycle)
    cycle.head = "c" * 40
    assert (await tick(cycle)).errors == 0
    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = 1
        plan.plan_document = document
        await db.commit()
    result = await tick(cycle)
    assert result.blocked == 1 and not cycle.calls
    assert len((await state(cycle))[3]) == 1


async def test_preparation_race_rolls_back_unstarted_retirement(cycle, monkeypatch):
    from src.orchestration.execution_runner import ObservationKind, RunnerContext
    from src.orchestration.review_cycle import CycleObservation

    old = await unstarted_intent(cycle)
    async with cycle.factory() as db:
        loaded = await load_execution(db, identity=cycle.identity)
    context = RunnerContext(cycle.identity, loaded.record, datetime.now(UTC))
    handler = ReviewCycleHandler(cycle.factory, cycle.service)
    effect = handler.decide(
        context,
        CycleObservation(
            ObservationKind.READY,
            operation_key=old.operation_key,
            snapshot={"replay": dict(old.detail)},
        ),
    ).effect
    original = cycle.service.observe_dispatch

    async def prepare_after_observation(current, action):
        observation = await original(current, action)
        # Simulate the other runner committing preparation after this runner's
        # preflight. It has not provisioned a credential or published to SQS yet.
        cycle.head = HEAD
        await cycle.service.prepare(context, effect)
        cycle.head = "c" * 40
        return observation

    cycle.head = "c" * 40
    monkeypatch.setattr(cycle.service, "observe_dispatch", prepare_after_observation)
    result = await tick(cycle)
    assert result.errors == 1 and not cycle.calls
    execution, claim, _, actions = await state(cycle)
    assert execution.pending_action_key == old.operation_key
    assert actions[0].status != "failed"
    assert claim.active_run_id == continuation_run_id(old.operation_key)


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


@pytest.mark.parametrize("failure", ["revoked_connection", "superseded_repository"])
async def test_accepted_repository_authority_is_rechecked_before_review(cycle, monkeypatch, failure):
    from src.admin.installations.resolver import OwnerState

    connection = {"provider": "github", "installation_id": 42}
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        node.repository_identity = cycle.binding.repo
        node.provider_repository_id = 123 if failure == "revoked_connection" else 999
        node.repository_connection = connection
        await db.commit()
    monkeypatch.setattr(
        "src.orchestration.accepted_node.accepted_node_contract",
        AsyncMock(
            return_value=SimpleNamespace(
                node=SimpleNamespace(
                    repository_identity=cycle.binding.repo,
                    provider_repository_id=123,
                    repository_connection=SimpleNamespace(model_dump=lambda **kwargs: connection),
                )
            )
        ),
    )
    owner = AsyncMock(return_value=(None, OwnerState.REVOKED))
    monkeypatch.setattr("src.admin.installations.resolver.resolve_installation_owner", owner)
    result = await tick(cycle)
    execution, claim, node, actions = await state(cycle)
    assert result.blocked == 1
    assert execution.block_code == ("authority_unverifiable" if failure == "revoked_connection" else "human_input_required")
    assert execution.block_detail == ("repository_connection_unavailable" if failure == "revoked_connection" else "bound_pr_or_scope_changed")
    assert not cycle.calls and not actions and node.attempts == 1 and claim.generation == 5
    assert owner.await_count == (1 if failure == "revoked_connection" else 0)


async def test_committed_dispatch_receipt_cannot_replay_after_budget_exhaustion(cycle):
    from src.orchestration.execution_runner import ObservationKind, RunnerContext
    from src.orchestration.review_cycle import CycleBlockedError, CycleObservation

    async def crash(name, context):
        if name == "after_effect":
            raise RuntimeError("lost receipt acknowledgement")

    assert (await tick(cycle, checkpoint=crash)).errors == 1
    execution, claim, node, actions = await state(cycle)
    assert len(actions) == 1 and len(cycle.calls) == 1
    context = RunnerContext(cycle.identity, execution, datetime.now(UTC))
    effect = (
        ReviewCycleHandler(cycle.factory, cycle.service)
        .decide(
            context, CycleObservation(ObservationKind.READY, operation_key=actions[0].operation_key, snapshot={"replay": dict(actions[0].detail)})
        )
        .effect
    )
    cycle.spend = Decimal(25)
    with pytest.raises(CycleBlockedError) as refused:
        await cycle.service.prepare(context, effect)
    assert refused.value.code.value == "budget_exhausted"
    assert len(cycle.calls) == 1 and (await state(cycle))[1].generation == claim.generation


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
    await legacy_delivery(cycle)
    await review(cycle, findings=[{"finding_id": "F1", "summary": "Correction required"}])
    assert (await tick(cycle)).effects_succeeded == 1
    await legacy_delivery(cycle)
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
    execution, _, _, actions = await state(ctx)
    assert result.effects_succeeded == 0 and len(ctx.calls) == 1
    assert actions[0].detail["startup_classification"]["failure_ordinal"] == 1
    assert 0 < (execution.next_check_at.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds() <= 61
    assert (await tick(ctx, force=False)).effects_succeeded == 0
    clock = SimpleNamespace(now=lambda: datetime.now(UTC) + timedelta(seconds=65), monotonic=lambda: 0.0)
    result = await tick(ctx, clock=clock, force=False)
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
    classification = actions[0].detail["startup_classification"]
    assert classification["original_invocation"] == failed_run
    assert classification["failure_category"] == "gateway_model_policy_refusal"
    assert classification["exemption_status"] == "bootstrap_exempt"
    assert classification["failure_ordinal"] == 1
    async with ctx.factory() as db:
        from src.orchestration.stage_attempts import stage_attempts, stage_counts

        assert await stage_attempts(db, org_id=ORG, node_id=node.id, action=Action.REVIEW) == 1
        assert (await stage_counts(db, org_id=ORG, node_ids=[node.id]))[node.id][Action.REVIEW.value] == 1
    assert claim.active_run_id == retry["message_id"]
    await review(ctx, approve=True)
    await tick(ctx)
    assert (await state(ctx))[0].phase == "merge_ready"


async def test_bootstrap_failures_block_with_cause_and_without_fourth_dispatch(cycle):
    ctx = cycle
    moment = [datetime.now(UTC) + timedelta(seconds=5)]
    clock = SimpleNamespace(now=lambda: moment[0], monotonic=lambda: 0.0)
    assert (await tick(ctx)).effects_succeeded == 1
    for ordinal, delay in enumerate((60, 300, None), start=1):
        run = ctx.calls[-1]["message_id"]
        raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{run}")
        if ordinal == 2:
            from src.agentauth.bootstrap_failure import record_refusal

            raw.pop("model_policy_snapshot", None)
            raw.pop("model_policy_snapshot_digest", None)
            raw.update(status={"S": "active"}, workload_binding={"S": "failed-bootstrap-pod"})
            ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
            record = ctx.store.authority.load_execution(invocation_id=run, tenant_id=ORG)
            record_refusal(
                ctx.store, record=record, request_id="gateway-refusal-second", events_table="cycle-events", observed_at=moment[0].isoformat()
            )
        else:
            raw.update(
                status={"S": "cancelled"},
                work_claim_cancellation={"S": "startup_deadline_exceeded"},
                work_claim_cancelled_at={"S": moment[0].isoformat()},
            )
            ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
        result = await tick(ctx, clock=clock)
        execution, claim, node, actions = await state(ctx)
        assert len(ctx.calls) == ordinal and len(actions) == ordinal
        assert actions[-1].detail["startup_classification"]["failure_ordinal"] == ordinal
        if ordinal == 2:
            assert actions[0].detail["startup_classification"]["fingerprint"] != actions[-1].detail["startup_classification"]["fingerprint"]
        assert node.attempts == 1 and claim.active_run_id == run
        async with ctx.factory() as db:
            from src.orchestration.stage_attempts import stage_attempts

            assert await stage_attempts(db, org_id=ORG, node_id=node.id, action=Action.REVIEW) == 0
        if delay is None:
            assert result.blocked == 1 and execution.block_code == "review_bootstrap_failing"
            assert "work_claim" in execution.block_detail and "deadline exceeded" in execution.block_detail
            assert (await tick(ctx, clock=clock, force=False)).examined == 0
            assert len(ctx.calls) == 3
        else:
            assert result.effects_succeeded == 0 and execution.status == "awaiting_external"
            assert execution.next_check_at.replace(tzinfo=UTC) == moment[0] + timedelta(seconds=delay)
            assert (await tick(ctx, clock=clock, force=False)).examined == 0
            moment[0] += timedelta(seconds=delay)
            assert (await tick(ctx, clock=clock, force=False)).effects_succeeded == 1
            assert len(ctx.calls) == ordinal + 1


async def test_unknown_startup_classification_keeps_reserved_attempt(cycle):
    from src.orchestration.stage_attempts import stage_attempts, stage_counts

    assert (await tick(cycle)).effects_succeeded == 1
    async with cycle.factory() as db:
        action = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "review_cycle_dispatch"))
        action.detail = {
            **action.detail,
            "startup_classification": {
                "version": "unrecognized",
                "failure_category": "gateway_model_policy_refusal",
                "exemption_status": "bootstrap_exempt",
            },
        }
        await db.commit()
    async with cycle.factory() as db:
        assert await stage_attempts(db, org_id=ORG, node_id=cycle.node.id, action=Action.REVIEW) == 1
        assert (await stage_counts(db, org_id=ORG, node_ids=[cycle.node.id]))[cycle.node.id][Action.REVIEW.value] == 1


async def test_worker_receipt_contradicts_startup_exemption(cycle):
    assert (await tick(cycle)).effects_succeeded == 1
    run = cycle.calls[-1]["message_id"]
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{run}")
    raw.update(status={"S": "cancelled"}, work_claim_cancellation={"S": "startup_deadline_exceeded"})
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    async with cycle.factory() as db:
        db.add(
            OrchestrationRunReport(
                run_id=run,
                credential_hash="a" * 64,
                org_id=ORG,
                flow_id=cycle.flow.id,
                node_id=cycle.node.id,
                attempt=1,
                persona="agent-codex-reviewer",
                repo=REPO,
                installation_id=42,
                dispatch_metadata={},
                worker_receipt={"recorded_at": datetime.now(UTC).isoformat()},
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await db.commit()
    result = await tick(cycle)
    execution, _, node, actions = await state(cycle)
    assert result.blocked == 1 and execution.block_code == "authority_unverifiable"
    assert len(actions) == len(cycle.calls) == 1
    assert "startup_classification" not in actions[0].detail
    async with cycle.factory() as db:
        from src.orchestration.stage_attempts import stage_attempts

        assert await stage_attempts(db, org_id=ORG, node_id=node.id, action=Action.REVIEW) == 1


@pytest.mark.parametrize("outer_timeout", [False, True])
async def test_resume_clears_bootstrap_counter_and_dispatches_once(cycle, monkeypatch, outer_timeout):
    from src.orchestration import controls
    from src.orchestration.lifecycle_recovery import ResumeContinuationRequest
    from src.orchestration.stage_attempts import stage_attempts

    ctx = cycle
    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: ctx.service.writer)
    moment = [datetime.now(UTC) + timedelta(seconds=5)]
    clock = SimpleNamespace(now=lambda: moment[0], monotonic=lambda: 0.0)
    if outer_timeout:
        from tests.orchestration.test_shared_lifecycle_recovery import timeout

        await timeout(ctx)
    else:
        assert (await tick(ctx)).effects_succeeded == 1
    for ordinal, delay in enumerate((60, 300, None), start=1):
        run = ctx.calls[-1]["message_id"]
        raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{run}")
        raw.update(
            status={"S": "cancelled"},
            work_claim_cancellation={"S": "startup_deadline_exceeded"},
            work_claim_cancelled_at={"S": moment[0].isoformat()},
        )
        ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
        if outer_timeout and ordinal == 1:
            from tests.orchestration.test_shared_lifecycle_recovery import recover, request

            async with ctx.factory() as db:
                assert (await recover(db, ctx, request(run)))["resumed"] is True
                await db.commit()
        await tick(ctx, clock=clock)
        if delay is not None:
            moment[0] += timedelta(seconds=delay)
            assert (await tick(ctx, clock=clock, force=False)).effects_succeeded == 1
    execution, _, node, actions = await state(ctx)
    assert execution.block_code == "review_bootstrap_failing"
    assert len(actions) == 3 and node.attempts == 1
    monkeypatch.setattr(
        "src.agentauth.human_control.authorize_human_session",
        AsyncMock(return_value=SimpleNamespace(tenant_id=ORG, user_id="human")),
    )
    access = SimpleNamespace(check_permission=AsyncMock(), get_user_role=AsyncMock(return_value=(SimpleNamespace(value="owner"),)))
    request = ResumeContinuationRequest(
        expected_attempt=1, expected_plan_version=1, expected_run_id=run, reason="Platform startup issue has been repaired"
    )
    results = []
    for _ in range(2):
        async with ctx.factory() as db:
            results.append(
                await controls.resume_current_continuation(
                    node_id=node.id, body=request, current_user=SimpleNamespace(org_id=ORG), access=access, db=db
                )
            )
    assert results[0]["resumed"] is True and results[1]["resumed"] is False
    assert results[0]["decision_id"] == results[1]["decision_id"]
    async with ctx.factory() as db:
        resets = list((await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "review_bootstrap_reset"))).all())
        assert len(resets) == 1 and resets[0].actor_id == "human"
        assert await stage_attempts(db, org_id=ORG, node_id=node.id, action=Action.REVIEW) == 0
    assert (await tick(ctx, clock=clock, force=False)).effects_succeeded == 1
    assert len(ctx.calls) == 4
    assert (await tick(ctx, clock=clock, force=False)).effects_succeeded == 0
    execution, _, node, actions = await state(ctx)
    assert node.attempts == 1 and len(actions) == 4
    async with ctx.factory() as db:
        assert await stage_attempts(db, org_id=ORG, node_id=node.id, action=Action.REVIEW) == 1


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


@pytest.mark.parametrize("allowance", [1, 2])
async def test_failed_reviewer_retries_retained_pr_with_remaining_allowance(cycle, allowance):
    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = allowance
        plan.plan_document = document
        await db.commit()
    assert (await tick(cycle)).effects_succeeded == 1
    failed_run = cycle.calls[-1]["message_id"]
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{failed_run}")
    raw.update(status={"S": "completed"}, terminal_outcome={"S": "failed"}, bootstrap_authority_issued_at={"S": "2026-09-20T00:00:00Z"})
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    # The accepted policy controls review retries even when the generic runner
    # default is lower; exhaustion of that policy must still refuse dispatch.
    result = await tick(cycle, runner_ceiling=1)
    execution, claim, node, actions = await state(cycle)
    assert node.attempts == 1 and claim.generation == 5
    assert len(actions) == allowance
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{failed_run}")["terminal_outcome"] == {"S": "failed"}
    if allowance == 1:
        assert result.effects_succeeded == 0
        assert execution.block_code == "attempts_exhausted"
        return
    assert result.effects_succeeded == 1
    assert actions[-1].detail["review_retry_of"] == failed_run
    assert cycle.calls[-1]["message_id"] != failed_run
    assert cycle.calls[-1]["review_expect"]["author_run_id"] == cycle.root
    await review(cycle, approve=True)
    await tick(cycle)
    assert (await state(cycle))[0].phase == "merge_ready"


@pytest.mark.parametrize("terminal", ["complete", "failed"])
async def test_protected_reviewer_owns_delivery_by_default_and_blockers_do_not_dispatch_another_pass(cycle, terminal):
    assert (await tick(cycle)).effects_succeeded == 1
    envelope = cycle.calls[-1]
    assert envelope["review_cycle_input"]["reviewer_owned_delivery"] is True
    await review(cycle, findings=[{"finding_id": "external", "summary": "Missing required evidence"}])
    if terminal == "failed":
        raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{envelope['message_id']}")
        raw["terminal_outcome"] = {"S": "failed"}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    result = await tick(cycle)
    execution, _, _, _ = await state(cycle)
    assert result.effects_attempted == 0
    assert len(cycle.calls) == 1
    assert execution.block_detail == "reviewer_delivery_blocked"


@pytest.mark.parametrize("evidence", ["none", "incomplete", "complete"])
@pytest.mark.parametrize("worker_status", ["pending", "active"])
async def test_running_reviewer_reads_provider_only_for_completed_evidence(cycle, monkeypatch, evidence, worker_status):
    from src.orchestration.pr_identity import PrIdentityError

    assert (await tick(cycle)).effects_succeeded == 1
    run = cycle.calls[-1]["message_id"]
    if evidence != "none":
        await review(cycle, approve=evidence == "complete")
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{run}")
    raw["status"] = {"S": worker_status}
    raw.pop("terminal_outcome", None)
    if worker_status == "active":
        raw["workload_binding"] = {"S": "live-reviewer-pod"}
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)

    provider = AsyncMock(side_effect=PrIdentityError("Provider rate limited"))
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_pr_identity", provider)
    result = await tick(cycle)
    execution, claim, _, actions = await state(cycle)
    assert result.effects_attempted == 0
    assert claim.active_run_id == run and len(actions) == 1
    if evidence == "complete":
        assert execution.block_code == "provider_unavailable"
        assert execution.block_detail == "pr_head_unavailable"
        provider.assert_awaited_once()
    else:
        assert execution.status == "awaiting_external"
        assert execution.block_code is None
        provider.assert_not_awaited()


async def test_reviewer_retries_at_depth_limit_preserve_authority_and_attempt_budget(cycle):
    ctx = cycle
    root_grant = ctx.store._read(f"TENANT#{ORG}", f"GRANT#{ctx.root}#1")
    root_grant["max_chain_depth"] = {"N": "1"}
    ctx.store.client.put_item(TableName=ctx.store.table, Item=root_grant)
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = 10
        plan.plan_document = document
        await db.commit()
    assert (await tick(ctx)).effects_succeeded == 1
    first = ctx.calls[-1]["message_id"]
    # Retry must not regain the ancestor's longer expiry or broader permissions.
    grant = ctx.store._read(f"TENANT#{ORG}", f"GRANT#{first}#1")
    expiry = (datetime.now(UTC) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    grant["expires_at"] = {"S": expiry}
    ctx.store.client.put_item(TableName=ctx.store.table, Item=grant)
    for index in range(10):
        run = ctx.calls[-1]["message_id"]
        raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{run}")
        raw.update(status={"S": "completed"}, terminal_outcome={"S": "failed"})
        ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
        result = await tick(ctx)
        if index == 9:
            assert result.effects_succeeded == 0
            assert (await state(ctx))[0].block_code == "attempts_exhausted"
            break
        assert result.effects_succeeded == 1
        retry = ctx.calls[-1]
        assert retry["correlation"]["chain_depth"] == 1
        assert retry["correlation"]["parent_principal"] == f"{ctx.root}#1"
        assert retry["review_expect"]["author_run_id"] == ctx.root
        live = ctx.store.live_grant(invocation_id=retry["message_id"], tenant_id=ORG, attempt=1, now=datetime.now(UTC))
        assert live.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") == expiry
        assert live.max_chain_depth == 1 and live.repo_scope == frozenset({REPO})
        assert {action.value for action in live.allowed_actions} == {"monitor"}
    assert len(ctx.calls) == 10
    assert len({call["message_id"] for call in ctx.calls}) == 10
    assert (await state(ctx))[2].attempts == 1


@pytest.mark.parametrize("change", ["missing", "revoked", "epoch", "depth"])
async def test_retry_lineage_refuses_missing_revoked_or_changed_parent(cycle, change):
    from src.orchestration.review_cycle import CycleBlockedError

    ctx = cycle
    assert (await tick(ctx)).effects_succeeded == 1
    run = ctx.calls[-1]["message_id"]
    raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{run}")
    grant = ctx.store.live_grant(invocation_id=run, tenant_id=ORG, attempt=1, now=datetime.now(UTC))
    if change == "missing":
        raw.pop("parent_principal")
    elif change == "epoch":
        raw["parent_grant_epoch"] = {"N": "999"}
    elif change == "depth":
        raw["chain_depth"] = {"N": "0"}
    else:
        parent = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{ctx.root}")
        parent["status"] = {"S": "revoked"}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=parent)
    with pytest.raises(CycleBlockedError, match="retry_parent_unverifiable"):
        await ctx.service.retry_parent(ORG, raw, grant)
    assert len(ctx.calls) == 1


@pytest.mark.parametrize("reason", ["spend_limit_exceeded", "budget_unavailable"])
async def test_budget_refusal_is_visible_and_retries_same_intent_after_recovery(cycle, reason):
    from src.orchestration.delivery_progress import node_progress
    from src.orchestration.execution_policy import DenyReason

    cycle.reserve.return_value = Decision.block(DenyReason(reason), "private provider detail")
    first = await tick(cycle)
    row, claim, node, actions = await state(cycle)
    assert first.blocked == 1
    assert row.status == "blocked" and row.block_code == "budget_exhausted"
    assert row.block_owner in {"flow-owner", "platform-operator"}
    assert row.block_required_input and "private provider detail" not in row.block_detail
    assert row.pending_action_key == actions[0].operation_key
    assert row.next_check_at is not None and not cycle.calls
    progress = node_progress(node=node, binding=cycle.binding, dispatch={}, result={}, execution=row, policy_enabled=True, plan_version=1)
    assert progress.blocker == "budget_exhausted" and progress.automation == "blocked"
    assert progress.next_action == row.block_required_input
    assert "Reviewing the current PR head" not in progress.detail
    first_key = row.pending_action_key
    await tick(cycle)
    row, _, _, actions = await state(cycle)
    assert row.status == "blocked" and row.pending_action_key == first_key
    assert len(actions) == 1 and not cycle.calls
    cycle.reserve.return_value = Decision.permit("budget restored")
    assert (await tick(cycle)).effects_succeeded == 1
    row, _, _, actions = await state(cycle)
    assert row.block_code is None and row.status != "blocked"
    assert len(actions) == 1
    assert len(cycle.calls) == 1
    assert cycle.calls[0]["message_id"] == continuation_run_id(first_key)
    async with cycle.factory() as db:
        from src.orchestration.stage_attempts import stage_attempts

        assert await stage_attempts(db, org_id=ORG, node_id=node.id, action=Action.REVIEW) == 1


async def test_nonbudget_admission_refusal_does_not_display_budget_hold(cycle):
    from src.orchestration.execution_policy import DenyReason

    cycle.reserve.return_value = Decision.block(DenyReason.CONCURRENCY_LIMIT_EXCEEDED, "busy")
    await tick(cycle)
    row, _, _, actions = await state(cycle)
    assert row.block_code is None
    assert row.progress_note == "concurrency_limit_exceeded"
    assert row.pending_action_key == actions[0].operation_key and not cycle.calls


async def test_budget_delayed_review_gets_fresh_startup_clock(cycle):
    from src.orchestration.execution_policy import DenyReason
    from src.orchestration.work_admission import cancel_unstarted_claim

    cycle.reserve.return_value = Decision.block(DenyReason.SPEND_LIMIT_EXCEEDED, "budget wait")
    await tick(cycle)
    _, _, _, actions = await state(cycle)
    assert len(actions) == 1 and not cycle.calls
    async with cycle.factory() as db:
        action = await db.get(OrchestrationAction, actions[0].id)
        action.detail = {**action.detail, "arrived_at": (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        await db.commit()
    cycle.reserve.return_value = Decision.permit("budget restored")
    before = datetime.now(UTC) - timedelta(seconds=1)
    assert (await tick(cycle)).effects_succeeded == 1
    envelope = cycle.calls[-1]
    assert datetime.fromisoformat(envelope["arrived_at"].replace("Z", "+00:00")) >= before
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{envelope['message_id']}")
    assert not cancel_unstarted_claim(cycle.store, raw, now=datetime.now(UTC))


@pytest.mark.parametrize("allowance", [1, 2])
async def test_startup_cancelled_review_retries_with_same_claim_and_bounded_attempts(cycle, allowance):
    from src.orchestration.work_admission import cancel_unstarted_claim, recover_exited_claims

    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = allowance
        plan.plan_document = document
        await db.commit()
    assert (await tick(cycle)).effects_succeeded == 1
    failed_run = cycle.calls[-1]["message_id"]
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{failed_run}")
    assert cancel_unstarted_claim(cycle.store, raw, now=datetime.now(UTC) + timedelta(hours=1))
    async with cycle.factory() as db:
        recovery = await recover_exited_claims(db, store=cycle.store, workloads=SimpleNamespace())
        await db.commit()
    assert recovery.released == 0
    result = await tick(cycle)
    execution, claim, node, actions = await state(cycle)
    assert node.attempts == 1 and claim.generation == 5
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{failed_run}")["status"] == {"S": "cancelled"}
    assert result.effects_succeeded == 0 and execution.status == "awaiting_external"
    assert len(actions) == 1 and actions[0].detail["startup_classification"]["exemption_status"] == "bootstrap_exempt"
    clock = SimpleNamespace(now=lambda: datetime.now(UTC) + timedelta(seconds=65), monotonic=lambda: 0.0)
    assert (await tick(cycle, clock=clock, force=False)).effects_succeeded == 1
    execution, claim, node, actions = await state(cycle)
    assert len(actions) == 2 and execution.attempts == 2
    retry = cycle.calls[-1]
    assert retry["message_id"] != failed_run
    assert retry["review_expect"]["author_run_id"] == cycle.root
    assert retry["correlation"]["parent_principal"] == f"{cycle.root}#1"
    assert claim.active_run_id == retry["message_id"]
    assert actions[-1].detail["bootstrap_retry_of"] == failed_run


@pytest.mark.parametrize("change", ["binding", "issued", "revoked", "unrelated_cancellation"])
async def test_startup_retry_refuses_other_cancellations(cycle, change):
    assert (await tick(cycle)).effects_succeeded == 1
    run = cycle.calls[-1]["message_id"]
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{run}")
    raw.update(status={"S": "cancelled"}, work_claim_cancellation={"S": "startup_deadline_exceeded"})
    if change == "binding":
        raw["workload_binding"] = {"S": "a-worker-existed"}
    elif change == "issued":
        raw["bootstrap_authority_issued_at"] = {"S": "2026-10-05T00:00:00Z"}
    elif change == "revoked":
        raw["status"] = {"S": "revoked"}
    else:
        raw["work_claim_cancellation"] = {"S": "operator_cancelled"}
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    assert (await tick(cycle)).effects_succeeded == 0
    assert len(cycle.calls) == 1
    _, _, node, actions = await state(cycle)
    assert "startup_classification" not in actions[0].detail
    async with cycle.factory() as db:
        from src.orchestration.stage_attempts import stage_attempts, stage_counts

        assert await stage_attempts(db, org_id=ORG, node_id=node.id, action=Action.REVIEW) == 1
        assert (await stage_counts(db, org_id=ORG, node_ids=[node.id]))[node.id][Action.REVIEW.value] == 1


# --- CI-failed owned delivery hands off to repair (#7457) -----------------------


def _ci_observation(state, checks):
    return json.dumps(
        {
            "source": "required-checks",
            "summary": "Canonical CI observation for this exact head",
            "evidence": {"contract_version": 1, "state": state, "reasons": ["required_check_failed"], "checks": checks},
        }
    )


def _delivery_blocked(state, checks):
    return {
        "finding_id": "delivery-blocked",
        "stage": "functional",
        "severity": "blocking",
        "disposition": "open",
        "summary": _ci_observation(state, checks),
    }


@pytest.mark.parametrize("terminal_outcome", ["complete", "failed"])
async def test_owned_delivery_blocked_only_by_failed_checks_dispatches_a_repair(cycle, terminal_outcome):
    """The reviewer approved the code and stopped because required checks failed.
    That is developer work within the accepted allowance, not a human decision."""
    assert (await tick(cycle)).effects_succeeded == 1
    assert cycle.calls[-1]["review_cycle_input"]["reviewer_owned_delivery"] is True
    checks = [{"name": "Lint", "state": "failure"}, {"name": "Unit Tests", "state": "pending"}, {"name": "Smoke", "state": "success"}]
    await review(cycle, findings=[_delivery_blocked("failed", checks)])
    run_id = cycle.calls[-1]["message_id"]
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{run_id}")
    raw["terminal_outcome"] = {"S": terminal_outcome}
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    result = await tick(cycle)
    execution, _, _, actions = await state(cycle)
    assert result.effects_succeeded == 1, execution.block_detail
    assert len(cycle.calls) == 2
    repair = cycle.calls[-1]["review_cycle_input"]
    assert repair["action"] == "repair"
    assert repair["findings"][0]["finding_id"] == "required-checks-failed"
    assert "Lint: failure" in repair["findings"][0]["summary"] and "Unit Tests" not in repair["findings"][0]["summary"]
    assert actions[-1].detail["action"] == "repair"


async def test_large_worker_ci_finding_survives_recording_and_failed_exit(cycle, monkeypatch):
    """Use the real worker normalizer before persisting the engine's review receipt."""
    path = Path(__file__).resolve().parents[4] / "modules/agent-factory/agent-worker-image/lib/review_result.py"
    spec = importlib.util.spec_from_file_location("worker_ci_review_result", path)
    worker = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, worker)
    spec.loader.exec_module(worker)
    checks = [{"name": f"Successful CI check {i}", "state": "success"} for i in range(120)]
    checks.append({"name": "gateway-ci / Test shard 8", "state": "failure"})
    findings = [asdict(f) for f in worker.findings_from_agent_report({"findings": [_delivery_blocked("failed", checks)]})]
    assert (await tick(cycle)).effects_succeeded == 1
    run_id = cycle.calls[-1]["message_id"]
    await review(cycle, findings=findings)
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{run_id}")
    raw["terminal_outcome"] = {"S": "failed"}
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    result = await tick(cycle)
    execution, _, node, actions = await state(cycle)
    assert result.effects_succeeded == 1, execution.block_detail
    assert node.attempts == 1 and actions[-1].detail["action"] == "repair"
    assert "gateway-ci / Test shard 8: failure" in cycle.calls[-1]["review_cycle_input"]["findings"][0]["summary"]


@pytest.mark.parametrize("terminal_outcome", ["complete", "failed"])
async def test_owned_delivery_waiting_on_pending_checks_is_not_a_repair(cycle, terminal_outcome):
    assert (await tick(cycle)).effects_succeeded == 1
    await review(cycle, findings=[_delivery_blocked("pending", [{"name": "Lint", "state": "pending"}])])
    run_id = cycle.calls[-1]["message_id"]
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{run_id}")
    raw["terminal_outcome"] = {"S": terminal_outcome}
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    result = await tick(cycle)
    execution, _, _, _ = await state(cycle)
    assert result.effects_attempted == 0 and len(cycle.calls) == 1
    assert execution.block_detail == "reviewer_delivery_blocked"


async def test_owned_delivery_with_a_defect_beside_failed_checks_repairs_both(cycle):
    """A classified defect is developer work (#7482); the red checks beside it ride along in the same repair."""
    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = 3
        plan.plan_document = document
        await db.commit()
    assert (await tick(cycle)).effects_succeeded == 1
    defect = {"finding_id": "F1", "stage": "functional", "severity": "blocking", "disposition": "open", "summary": "Null dereference"}
    await review(cycle, findings=[defect, _delivery_blocked("failed", [{"name": "Lint", "state": "failure"}])])
    result = await tick(cycle)
    execution, _, _, actions = await state(cycle)
    assert result.effects_succeeded == 1, execution.block_detail
    assert actions[-1].detail["action"] == "repair"
    findings = cycle.calls[-1]["review_cycle_input"]["findings"]
    assert [f["finding_id"] for f in findings] == ["F1", "required-checks-failed"]
    assert "Lint: failure" in findings[1]["summary"]


def test_failing_required_checks_parser_is_strict():
    from src.orchestration.review_cycle import failing_required_checks

    assert failing_required_checks(None) is None
    assert failing_required_checks([]) is None
    assert failing_required_checks([{"finding_id": "delivery-blocked", "summary": "not json"}]) is None
    assert failing_required_checks([_delivery_blocked("failed", [{"name": "Lint", "state": "failure"}])]) == ["Lint: failure"]
    assert failing_required_checks([_delivery_blocked("failed", [{"name": "Lint", "state": "pending"}])]) is None
    resolved = {**_delivery_blocked("failed", [{"name": "Lint", "state": "failure"}]), "disposition": "resolved"}
    assert failing_required_checks([resolved]) is None


async def test_refused_publication_is_retried_not_treated_as_a_delivered_review(cycle):
    """A reviewer that could not publish its verdict leaves the review undone (#7468)."""
    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = 3
        plan.plan_document = document
        await db.commit()
    assert (await tick(cycle)).effects_succeeded == 1
    failed_run = cycle.calls[-1]["message_id"]
    assert cycle.calls[-1]["review_cycle_input"]["reviewer_owned_delivery"] is True
    # The run recorded an approve verdict whose publication the provider refused,
    # then exited failed (adp-review EXIT_FAILED / pending approval).
    await review(cycle, approve=False, publication=True)
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{failed_run}")
    raw.update(status={"S": "completed"}, terminal_outcome={"S": "failed"}, bootstrap_authority_issued_at={"S": "2026-09-20T00:00:00Z"})
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    result = await tick(cycle)
    execution, _, node, actions = await state(cycle)
    assert result.effects_succeeded == 1, execution.block_detail
    assert actions[-1].detail["review_retry_of"] == failed_run
    assert cycle.calls[-1]["message_id"] != failed_run and node.attempts == 1


async def test_failed_reviewer_owned_repair_retries_instead_of_stopping_for_a_human(cycle):
    """A reviewer-owned repair that exits failed is a retryable reviewer continuation.

    TLS-C05 tonight: the reviewer pushed its repair, could not publish the verdict,
    exited failed, and the cycle blocked worker_failed_or_halted for a human
    although repair attempts remained.
    """
    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = 3
        plan.plan_document = document
        await db.commit()
    assert (await tick(cycle)).effects_succeeded == 1
    defect = {
        "finding_id": "R2",
        "stage": "functional",
        "severity": "blocking",
        "disposition": "open",
        "summary": "Gate release on smoke",
        "evidence_refs": [],
    }
    await review(cycle, findings=[defect])
    assert (await tick(cycle)).effects_succeeded == 1
    repair_envelope = cycle.calls[-1]
    assert repair_envelope["review_cycle_input"]["action"] == "repair"
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{repair_envelope['message_id']}")
    raw.update(status={"S": "completed"}, terminal_outcome={"S": "failed"})
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    result = await tick(cycle)
    execution, _, node, actions = await state(cycle)
    assert result.effects_succeeded == 1, execution.block_detail
    assert actions[-1].detail["review_retry_of"] == repair_envelope["message_id"]
    assert cycle.calls[-1]["message_id"] != repair_envelope["message_id"]
    assert node.attempts == 1


async def test_open_blocking_defect_from_a_failed_reviewer_exit_hands_off_to_bounded_repair(cycle):
    """The reviewer recorded a classified open defect and then exited failed.

    Before, the review retry found its own exact-head evidence and stopped for a
    human (`reviewer_delivery_blocked`) even though the same finding from a clean
    exit is handed to a bounded developer repair (#7482). The exit code of the
    reviewer process does not change whose work the defect is.
    """
    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = 3
        plan.plan_document = document
        await db.commit()
    assert (await tick(cycle)).effects_succeeded == 1
    envelope = cycle.calls[-1]
    assert envelope["review_cycle_input"]["reviewer_owned_delivery"] is True
    defect = {
        "finding_id": "TLS-C05-R2",
        "stage": "functional",
        "severity": "blocking",
        "disposition": "open",
        "summary": "Gate release publication on actual MCP smoke results",
        "evidence_refs": [],
    }
    await review(cycle, findings=[defect])
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{envelope['message_id']}")
    raw["terminal_outcome"] = {"S": "failed"}
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    result = await tick(cycle)
    execution, _, node, actions = await state(cycle)
    assert result.effects_succeeded == 1, execution.block_detail
    assert actions[-1].detail["action"] == "repair"
    assert cycle.calls[-1]["review_cycle_input"]["findings"][0]["finding_id"] == "TLS-C05-R2"
    assert node.attempts == 1


async def test_open_blocking_defect_after_reviewer_owned_pass_hands_off_to_bounded_repair(cycle):
    """A classified defect the reviewer could not fix is developer work while repair attempts remain (#7482)."""
    async with cycle.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = 3
        plan.plan_document = document
        await db.commit()
    assert (await tick(cycle)).effects_succeeded == 1
    assert cycle.calls[-1]["review_cycle_input"]["reviewer_owned_delivery"] is True
    defect = {
        "finding_id": "COS-C01-REV-02",
        "stage": "functional",
        "severity": "blocking",
        "disposition": "open",
        "summary": "[P1] Reconcile engine connection inputs with the permitted deployment path",
        "evidence_refs": [],
    }
    await review(cycle, findings=[defect])
    result = await tick(cycle)
    execution, _, node, actions = await state(cycle)
    assert result.effects_succeeded == 1, execution.block_detail
    assert actions[-1].detail["action"] == "repair"
    assert cycle.calls[-1]["review_cycle_input"]["findings"][0]["finding_id"] == "COS-C01-REV-02"
    assert node.attempts == 1


async def test_terminal_reviewer_budget_failure_is_visible_without_burning_retries(cycle):
    from src.orchestration.delivery_progress import node_progress
    from src.orchestration.stage_attempts import stage_attempts

    assert (await tick(cycle)).effects_succeeded == 1
    run = cycle.calls[-1]["message_id"]
    raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{run}")
    raw.update(
        status={"S": "completed"},
        terminal_outcome={"S": "budget_stopped"},
        terminal_recorded_at={"S": datetime.now(UTC).isoformat()},
    )
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    async with cycle.factory() as db:
        assert await db.get(OrchestrationRunReport, run) is None
    for _ in range(2):
        result = await tick(cycle)
        row, claim, node, actions = await state(cycle)
        assert result.blocked == 1 and result.effects_attempted == 0
        assert row.status == "blocked" and row.block_code == "budget_exhausted"
        assert "reviewer stopped" in row.block_detail.lower()
        assert row.next_check_at is not None
        assert len([action for action in actions if action.kind == "review_cycle_dispatch"]) == 1
        assert len(cycle.calls) == 1
        assert node.attempts == 1 and claim.generation == 5
        async with cycle.factory() as db:
            assert await stage_attempts(db, org_id=ORG, node_id=node.id, action=Action.REVIEW) == 1
        progress = node_progress(node=node, binding=cycle.binding, dispatch={}, result={}, execution=row, policy_enabled=True, plan_version=1)
        assert progress.blocker == "budget_exhausted" and progress.automation == "blocked"
        assert "chain" in progress.next_action and "daily" in progress.next_action
