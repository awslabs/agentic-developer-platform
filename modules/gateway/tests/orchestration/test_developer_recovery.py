"""Settled development failures retry once per authorized attempt, never live work."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.orchestration.developer_recovery import recover_developer, recover_failed_developers, retry_context
from src.orchestration.flow_meter import reconcile_flow_meter as real_reconcile_flow_meter
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
from src.orchestration.results import observe_results
from src.orchestration.run_reports import prepare_run_report, record_terminal
from src.orchestration.stall import StallConfig, _Candidate, _fence_unacknowledged_assignment, detect_stalls
from src.orchestration.state import ActorKind, NodeState, transition
from tests.orchestration.test_review_cycle import ORG, REPO, cycle, pg_server, pg_url, store  # noqa: F401


@pytest.mark.parametrize(
    "counterexample",
    [
        None,
        "bound",
        "registered",
        "missing",
        "wrong_tenant",
        "receipt",
        "pr",
        "issued",
        "prior_registration",
        "concurrent",
    ],
)
async def test_pending_protected_fence_requires_exact_unstarted_authority(cycle, counterexample):  # noqa: F811
    async with cycle.factory() as db:
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        execution.handoff_receipt_ref = None
        if counterexample != "pr":
            await db.delete(await db.get(OrchestrationPullRequestBinding, cycle.binding.id))
        node = await db.get(OrchestrationNode, cycle.node.id)
        candidate = _Candidate(
            node_id=node.id,
            org_id=node.org_id,
            flow_id=node.flow_id,
            observed_state=node.state,
            attempts=node.attempts,
            since=datetime.now(UTC),
        )
        await db.commit()
    raw = {
        "pk": {"S": f"TENANT#{ORG}"},
        "sk": {"S": f"EXEC#{cycle.root}"},
        "tenant_id": {"S": "other" if counterexample == "wrong_tenant" else ORG},
        "invocation_id": {"S": cycle.root},
        "flow_id": {"S": cycle.flow.id},
        "orchestration_node_id": {"S": cycle.node.id},
        "orchestration_node_attempt": {"N": "1"},
        "status": {"S": "pending"},
        "current_attempt": {"N": "1"},
        "persona": {"S": "developer"},
        "arrived_at": {"S": datetime.now(UTC).isoformat()},
    }
    if counterexample == "bound":
        raw["workload_binding"] = {"S": "pod"}
    if counterexample == "issued":
        raw["bootstrap_authority_issued_at"] = {"S": "issued"}
    if counterexample == "prior_registration":
        raw["current_attempt"] = {"N": "2"}
    if counterexample != "missing":
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    else:
        cycle.store.client.delete_item(
            TableName=cycle.store.table,
            Key={"pk": raw["pk"], "sk": raw["sk"]},
        )
    if counterexample in {"registered", "prior_registration"}:
        cycle.store.client.put_item(
            TableName=cycle.store.table,
            Item={"pk": raw["pk"], "sk": {"S": f"REG#{cycle.root}#1"}},
        )
    if counterexample == "receipt":
        from src.orchestration.run_reports import OrchestrationRunReport

        async with cycle.factory() as db:
            db.add(
                OrchestrationRunReport(
                    run_id=cycle.root,
                    credential_hash="f" * 64,
                    org_id=ORG,
                    flow_id=cycle.flow.id,
                    node_id=cycle.node.id,
                    attempt=1,
                    persona="developer",
                    repo=REPO,
                    installation_id=42,
                    dispatch_metadata={},
                    worker_receipt={"recorded_at": "observed"},
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            await db.commit()
    if counterexample == "concurrent":
        from botocore.exceptions import ClientError

        # The interleaving under test: a worker bootstrap lands AFTER the fence has
        # read the pending assignment and BEFORE its conditional transaction
        # commits. Real DynamoDB serializes the two conditional writes so exactly
        # one wins; moto does not serialize concurrent callers from different
        # threads, so a thread race here produced impossible states (both won, or
        # the loser's write persisted) roughly one run in three (#7630). Injecting
        # the bootstrap inside the fence's transaction call pins that interleaving
        # deterministically; the fence-first interleaving is the default case above
        # (fence cancels, late bootstrap is refused with ClientError).
        transact = cycle.store.client.transact_write_items
        outcome = {}

        def late_bootstrap():
            try:
                cycle.store.client.update_item(
                    TableName=cycle.store.table,
                    Key={"pk": raw["pk"], "sk": raw["sk"]},
                    UpdateExpression="SET #st = :active, workload_binding = :pod",
                    ConditionExpression="#st = :pending AND attribute_not_exists(workload_binding)",
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={":pending": {"S": "pending"}, ":active": {"S": "active"}, ":pod": {"S": "late-pod"}},
                )
                return True
            except ClientError:
                return False

        def racing_transaction(**kwargs):
            outcome["bound"] = late_bootstrap()
            return transact(**kwargs)

        cycle.store.client.transact_write_items = racing_transaction
        async with cycle.factory() as db:
            receipt = await _fence_unacknowledged_assignment(db, candidate, store=cycle.store)
            await db.commit()
        bound = outcome["bound"]
        assert bound is True
        persisted = cycle.store._read(raw["pk"]["S"], raw["sk"]["S"])
        async with cycle.factory() as db:
            claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
            assert receipt is None
            assert claim.state == "held"
            assert persisted["status"] == {"S": "active"}
            assert persisted["workload_binding"] == {"S": "late-pod"}
        return
    async with cycle.factory() as db:
        receipt = await _fence_unacknowledged_assignment(db, candidate, store=cycle.store)
        await db.commit()
    persisted = cycle.store._read(raw["pk"]["S"], raw["sk"]["S"])
    async with cycle.factory() as db:
        claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
        if counterexample is None:
            assert receipt and receipt["run_id"] == cycle.root
            assert persisted["status"] == {"S": "cancelled"}
            assert persisted["work_claim_cancellation"] == {"S": "unacknowledged_stall"}
            assert claim.state == "released" and claim.generation == 5
            from botocore.exceptions import ClientError

            with pytest.raises(ClientError):
                cycle.store.client.update_item(
                    TableName=cycle.store.table,
                    Key={"pk": raw["pk"], "sk": raw["sk"]},
                    UpdateExpression="SET #st = :active, workload_binding = :pod",
                    ConditionExpression="#st = :pending AND attribute_not_exists(workload_binding)",
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={":active": {"S": "active"}, ":pod": {"S": "late-pod"}, ":pending": {"S": "pending"}},
                )
        else:
            assert receipt is None
            assert claim.state == "held"
            if persisted is not None:
                assert persisted["status"] == {"S": "pending"}


@pytest.mark.parametrize(
    "age,stalled,repeat,with_report",
    [
        (1199, False, False, False),
        (1200, True, False, False),
        (1200, True, True, False),
        (1200, True, False, True),
    ],
)
async def test_never_acknowledged_threshold(cycle, monkeypatch, age, stalled, repeat, with_report, pg_url):  # noqa: F811
    from src.orchestration.dispatch_pass import attempt_run_id
    from src.orchestration.execution_state import ExecutionIdentity
    from src.orchestration.execution_store import create_execution
    from src.orchestration.run_reports import OrchestrationRunReport

    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: type("Writer", (), {"store": cycle.store})())
    now = datetime.now(UTC)
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        node.updated_at = now - timedelta(seconds=age)
        (await db.get(OrchestrationExecution, cycle.execution.id)).handoff_receipt_ref = None
        await db.delete(await db.get(OrchestrationPullRequestBinding, cycle.binding.id))
        if with_report:
            db.add(
                OrchestrationRunReport(
                    run_id=cycle.root,
                    credential_hash="f" * 64,
                    org_id=ORG,
                    flow_id=cycle.flow.id,
                    node_id=node.id,
                    attempt=1,
                    persona="developer",
                    repo=REPO,
                    installation_id=42,
                    dispatch_metadata={
                        "message_id": cycle.root,
                        "tenant_id": ORG,
                        "orchestration": {"flow_id": cycle.flow.id, "node_id": node.id, "attempt": 1},
                    },
                    expires_at=now + timedelta(days=1),
                )
            )
        if repeat:
            from src.orchestration.continuation import digest

            plan = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == cycle.flow.id))
            document = {
                **plan.plan_document,
                "nodes": [
                    {"address": "cycle/E1/W1/N1", "kind": "story", "issue_ref": "43"},
                ],
                "edges": [],
            }
            document["execution_policy"] = {
                **document["execution_policy"],
                "limits": {
                    **document["execution_policy"]["limits"],
                    "max_attempts_per_node": 2,
                },
            }
            plan.plan_document, plan.plan_hash = document, digest(document)
        db.add(
            OrchestrationDecision(
                org_id=ORG,
                flow_id=cycle.flow.id,
                node_id=cycle.node.id,
                kind="node_dispatched",
                actor_id="system:orchestration-dispatch",
                actor_kind="service",
                actor_role="engine",
                created_at=now - timedelta(seconds=age),
                reason=json.dumps({"attempt": 1, "run_id": attempt_run_id(node.id, 1)}),
            )
        )
        await db.commit()
    cycle.store.client.put_item(
        TableName=cycle.store.table,
        Item={
            "pk": {"S": f"TENANT#{ORG}"},
            "sk": {"S": f"EXEC#{cycle.root}"},
            "tenant_id": {"S": ORG},
            "invocation_id": {"S": cycle.root},
            "flow_id": {"S": cycle.flow.id},
            "orchestration_node_id": {"S": cycle.node.id},
            "orchestration_node_attempt": {"N": "1"},
            "current_attempt": {"N": "1"},
            "status": {"S": "pending"},
            "persona": {"S": "developer"},
            "arrived_at": {"S": now.isoformat()},
        },
    )
    meter_client = None
    if stalled and not repeat:
        import fakeredis.aioredis

        from src.budget.config import budget_config
        from src.budget.reservations import ReservationStore
        from src.orchestration import flow_budget
        from src.orchestration.flow_meter import meter_target
        from src.shared.models.base import Base
        from src.shared.models.budget import BudgetSettlementReceipt

        async with cycle.factory.kw["bind"].begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[BudgetSettlementReceipt.__table__]))
        meter_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        meter_store = ReservationStore(redis_url=None, ttl_seconds=172800, client=meter_client, clock=lambda: 1000.0)
        monkeypatch.setattr(flow_budget, "_reservations", meter_store)
        monkeypatch.setattr(budget_config, "budget_reservation_enabled", True)
        monkeypatch.setattr("src.orchestration.flow_meter.reconcile_flow_meter", real_reconcile_flow_meter)
        target = meter_target(org_id=ORG, flow_id=cycle.flow.id, policy=cycle.policy)
        from dataclasses import replace
        from decimal import Decimal

        assert (await meter_store.reserve("__initialized__", Decimal(0), [replace(target, require_initialization=False)])).admitted
        assert (await meter_store.reserve("stale-developer-model-call", Decimal(2), [target])).admitted
        initial_reservations = await meter_client.hgetall(target.key())
        assert (await meter_store.snapshot(target)).total_usd == Decimal(2)
    async with cycle.factory() as db:
        outcome = await detect_stalls(db, now, config=StallConfig(never_acknowledged_seconds=1200))
        await db.commit()
        assert outcome.stalls_detected == int(stalled)
    if not stalled:
        async with cycle.factory() as db:
            assert (await db.get(OrchestrationNode, cycle.node.id)).state == "running"
            assert (await db.get(OrchestrationWorkClaim, cycle.claim.id)).state == "held"
        assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}")["status"] == {"S": "pending"}
        return
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        decision = await db.scalar(
            select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "node_stalled")
        )
        assert node.state == "failed" and node.attempts == 1
        assert json.loads(decision.reason)["fence"] == "unacknowledged_stall"
        assert (await db.get(OrchestrationExecution, cycle.execution.id)).status == "concluded"
        assert (await db.get(OrchestrationWorkClaim, cycle.claim.id)).state == "released"
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}")["status"] == {"S": "cancelled"}
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        decision = await db.scalar(
            select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "node_stalled")
        )
        result = await recover_developer(db, node, now=decision.created_at.replace(tzinfo=UTC) + timedelta(seconds=61))
        assert result == "scheduled"
        await db.commit()
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        assert node.state == "ready" and node.attempts == 1
        retry = await retry_context(db, node, 1)
        assert retry["reason"] == "stall_retry" and retry["previous_run_id"] == cycle.root
        assert retry["fence_receipt"] == {"claim_id": cycle.claim.id, "claim_generation": 5, "run_id": cycle.root}
        assert retry["failure_decision_id"] == decision.id and retry["execution_id"] == cycle.execution.id
    if not repeat:
        from unittest.mock import AsyncMock

        from src.orchestration.dispatch_pass import DispatchPassConfig, run_dispatch_pass
        from src.orchestration.execution_policy import Decision
        from src.shared.models.base import Base

        async with cycle.factory.kw["bind"].begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[OrchestrationEdge.__table__]))
        monkeypatch.setattr("src.shared.identity.resolver.resolve_root_user_entity_id", AsyncMock(return_value="human"))
        monkeypatch.setattr("src.shared.identity.resolver.resolve_user_entity_id", AsyncMock(return_value="sub"))
        monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=42))
        monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=123))
        monkeypatch.setattr("src.orchestration.dispatch_pass.authorize_node_dispatch", AsyncMock(return_value=Decision.permit("accepted repair")))
        async with cycle.factory() as db:
            config = DispatchPassConfig(queue_url="https://sqs.test/cycle.fifo", repo=REPO)
            dispatched = await run_dispatch_pass(db, config)
            assert dispatched.dispatched == 1, dispatched
            node = await db.get(OrchestrationNode, cycle.node.id)
            assert node.attempts == 2 and node.state == "running"
            assert len(dispatched.pending) == 1
            assert dispatched.pending[0].envelope["orchestration"]["developer_recovery"]["reason"] == "stall_retry"
            assert dispatched.pending[0].envelope["orchestration"]["developer_recovery"]["fence_receipt"] == retry["fence_receipt"]
            await db.commit()
            dispatched_attempts = (
                await db.scalars(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.node_id == cycle.node.id, OrchestrationDecision.kind == "node_dispatched"
                    )
                )
            ).all()
            assert len(dispatched_attempts) == 2
        assert await meter_client.hgetall(target.key()) == initial_reservations
        await meter_store.close()
        return
    if repeat:
        from src.orchestration.continuation import digest
        from src.orchestration.plan_lineage import execution_plan_matches

        async with cycle.factory() as db:
            base = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == cycle.flow.id))
            amendment = OrchestrationDecision(
                org_id=ORG,
                flow_id=cycle.flow.id,
                kind="plan_amended",
                actor_kind="human",
                actor_id="human",
                actor_role="owner",
                reason="Preserve the running story while adding a gate",
            )
            db.add(amendment)
            await db.flush()
            document = {
                **base.plan_document,
                "nodes": [
                    *base.plan_document["nodes"],
                    {"address": "cycle/E2/W1/G1", "kind": "gate", "issue_ref": "44"},
                ],
            }
            db.add(
                OrchestrationAcceptedPlan(
                    org_id=ORG,
                    flow_id=cycle.flow.id,
                    version=2,
                    plan_document=document,
                    plan_hash=digest(document),
                    accepted_by_decision_id=amendment.id,
                )
            )
            base.superseded_at = now
            await db.commit()
            assert await execution_plan_matches(
                db,
                org_id=ORG,
                flow_id=cycle.flow.id,
                node_id=cycle.node.id,
                version=1,
                current_version=2,
            )
        run_id = attempt_run_id(cycle.node.id, 2)
        async with cycle.factory() as db:
            node = await db.get(OrchestrationNode, cycle.node.id)
            claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
            node.state, node.attempts, node.updated_at = "running", 2, now - timedelta(seconds=1200)
            claim.state, claim.generation, claim.active_run_id, claim.claim_event_id = "held", 6, run_id, run_id
            claim.release_reason, claim.released_at = None, None
            await db.flush()
            await create_execution(
                db,
                identity=ExecutionIdentity(ORG, node.id, 2, 1, claim.id, 6),
                flow_id=node.flow_id,
                next_check_at=now - timedelta(seconds=1200),
            )
            db.add(
                OrchestrationDecision(
                    org_id=ORG,
                    flow_id=node.flow_id,
                    node_id=node.id,
                    kind="node_dispatched",
                    actor_id="system:orchestration-dispatch",
                    actor_kind="service",
                    actor_role="engine",
                    created_at=now - timedelta(seconds=1199),
                    reason=json.dumps({"attempt": 2, "run_id": run_id}),
                )
            )
            await db.commit()
        cycle.store.client.put_item(
            TableName=cycle.store.table,
            Item={
                "pk": {"S": f"TENANT#{ORG}"},
                "sk": {"S": f"EXEC#{run_id}"},
                "tenant_id": {"S": ORG},
                "invocation_id": {"S": run_id},
                "flow_id": {"S": cycle.flow.id},
                "orchestration_node_id": {"S": cycle.node.id},
                "orchestration_node_attempt": {"N": "2"},
                "current_attempt": {"N": "1"},
                "status": {"S": "pending"},
                "persona": {"S": "developer"},
                "arrived_at": {"S": now.isoformat()},
            },
        )
        async with cycle.factory() as db:
            outcome = await detect_stalls(db, now + timedelta(seconds=1))
            await db.commit()
            assert outcome.stalls_detected == 1
        async with cycle.factory() as db:
            node = await db.get(OrchestrationNode, cycle.node.id)
            assert await recover_developer(db, node, now=now + timedelta(seconds=120)) == "failure_requires_operator"
            assert await recover_developer(db, node, now=now + timedelta(seconds=121)) == "failure_requires_operator"
            await db.commit()
            blocks = (
                await db.scalars(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "developer_retry_blocked"
                    )
                )
            ).all()
            retries = (
                await db.scalars(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "developer_retry_scheduled"
                    )
                )
            ).all()
            assert len(blocks) == 1 and blocks[0].reason == "worker vanished twice"
            assert len(retries) == 1 and node.state == "failed" and node.attempts == 2
        from src.orchestration.dispatch_pass import DispatchPassConfig, run_dispatch_pass
        from src.shared.models.base import Base

        async with cycle.factory.kw["bind"].begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[OrchestrationEdge.__table__]))
        async with cycle.factory() as db:
            dispatched = await run_dispatch_pass(db, DispatchPassConfig(queue_url="https://sqs.test/cycle.fifo", repo=REPO))
            await db.commit()
            assert dispatched.dispatched == 0 and not dispatched.pending
            assert (await db.get(OrchestrationNode, cycle.node.id)).attempts == 2
            assert (
                len(
                    (
                        await db.scalars(
                            select(OrchestrationDecision).where(
                                OrchestrationDecision.node_id == cycle.node.id, OrchestrationDecision.kind == "node_dispatched"
                            )
                        )
                    ).all()
                )
                == 2
            )
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from tests.migrations.conftest_postgres import to_async_url

        restarted_engine = create_async_engine(to_async_url(pg_url))
        restarted_factory = async_sessionmaker(restarted_engine, expire_on_commit=False)
        try:
            for tick in (122, 123):
                async with restarted_factory() as db:
                    node = await db.get(OrchestrationNode, cycle.node.id)
                    assert await recover_developer(db, node, now=now + timedelta(seconds=tick)) == "failure_requires_operator"
                    await db.commit()
                    assert node.state == "failed" and node.attempts == 2
                    decisions = (
                        await db.scalars(
                            select(OrchestrationDecision).where(
                                OrchestrationDecision.node_id == node.id,
                                OrchestrationDecision.kind.in_(
                                    [
                                        "developer_retry_scheduled",
                                        "developer_retry_blocked",
                                        "node_stalled",
                                        "node_dispatched",
                                    ]
                                ),
                            )
                        )
                    ).all()
                    assert {
                        kind: sum(row.kind == kind for row in decisions)
                        for kind in (
                            "developer_retry_scheduled",
                            "developer_retry_blocked",
                            "node_stalled",
                            "node_dispatched",
                        )
                    } == {
                        "developer_retry_scheduled": 1,
                        "developer_retry_blocked": 1,
                        "node_stalled": 2,
                        "node_dispatched": 2,
                    }
                    scheduled = next(row for row in decisions if row.kind == "developer_retry_scheduled")
                    blocked = next(row for row in decisions if row.kind == "developer_retry_blocked")
                    assert json.loads(scheduled.reason)["reason"] == "stall_retry"
                    assert json.loads(scheduled.reason)["previous_attempt"] == 1
                    assert blocked.reason == "worker vanished twice"
        finally:
            await restarted_engine.dispose()


@pytest.mark.parametrize("case", ["paused", "worker_receipt", "workload_binding", "missing_exec"])
async def test_never_acknowledged_early_tick_refuses_unverified_or_paused_assignment(cycle, monkeypatch, case):  # noqa: F811
    from src.orchestration.dispatch_pass import attempt_run_id
    from src.orchestration.run_reports import OrchestrationRunReport

    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: type("Writer", (), {"store": cycle.store})())
    now = datetime.now(UTC)
    age = StallConfig().stall_threshold_seconds + 1 if case == "paused" else 1200
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        node.updated_at = now - timedelta(seconds=age)
        (await db.get(OrchestrationExecution, cycle.execution.id)).handoff_receipt_ref = None
        await db.delete(await db.get(OrchestrationPullRequestBinding, cycle.binding.id))
        if case == "paused":
            (await db.get(OrchestrationFlow, cycle.flow.id)).execution_paused = True
        if case == "worker_receipt":
            db.add(
                OrchestrationRunReport(
                    run_id=cycle.root,
                    credential_hash="f" * 64,
                    org_id=ORG,
                    flow_id=cycle.flow.id,
                    node_id=node.id,
                    attempt=1,
                    persona="developer",
                    repo=REPO,
                    installation_id=42,
                    dispatch_metadata={},
                    worker_receipt={"recorded_at": (now - timedelta(seconds=age)).isoformat()},
                    expires_at=now + timedelta(days=1),
                )
            )
        db.add(
            OrchestrationDecision(
                org_id=ORG,
                flow_id=cycle.flow.id,
                node_id=node.id,
                kind="node_dispatched",
                actor_id="system:orchestration-dispatch",
                actor_kind="service",
                actor_role="engine",
                created_at=now - timedelta(seconds=age),
                reason=json.dumps({"attempt": 1, "run_id": attempt_run_id(node.id, 1)}),
            )
        )
        await db.commit()
    raw = {
        "pk": {"S": f"TENANT#{ORG}"},
        "sk": {"S": f"EXEC#{cycle.root}"},
        "tenant_id": {"S": ORG},
        "invocation_id": {"S": cycle.root},
        "flow_id": {"S": cycle.flow.id},
        "orchestration_node_id": {"S": cycle.node.id},
        "orchestration_node_attempt": {"N": "1"},
        "current_attempt": {"N": "1"},
        "status": {"S": "pending"},
        "persona": {"S": "developer"},
        "arrived_at": {"S": now.isoformat()},
    }
    if case == "workload_binding":
        raw["workload_binding"] = {"S": "active-pod"}
    if case == "missing_exec":
        cycle.store.client.delete_item(TableName=cycle.store.table, Key={"pk": raw["pk"], "sk": raw["sk"]})
    else:
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    async with cycle.factory() as db:
        outcome = await detect_stalls(db, now, config=StallConfig(never_acknowledged_seconds=1200))
        await db.commit()
        assert outcome.stalls_detected == 0 and outcome.errors == 0, outcome
        node = await db.get(OrchestrationNode, cycle.node.id)
        assert node.state == "running" and node.attempts == 1
        assert (await db.get(OrchestrationWorkClaim, cycle.claim.id)).state == "held"
        assert not (
            await db.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == node.id,
                    OrchestrationDecision.kind == "node_stalled",
                )
            )
        ).all()
    if case == "missing_exec":
        assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}") is None
    else:
        assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}")["status"] == {"S": "pending"}


@pytest.mark.parametrize("case", ["unstarted", "bound", "registered", "missing_exec"])
@pytest.mark.parametrize("paused", [False, True])
async def test_resumed_protected_assignment_uses_fresh_unacknowledged_deadline(cycle, monkeypatch, case, paused):  # noqa: F811
    from src.orchestration.dispatch_pass import attempt_run_id

    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: type("Writer", (), {"store": cycle.store})())
    now = datetime.now(UTC)
    old_dispatch = now - timedelta(seconds=StallConfig().stall_threshold_seconds + 2000)
    resumed_at = now - timedelta(seconds=1199)
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        node.updated_at = old_dispatch
        if paused:
            (await db.get(OrchestrationFlow, cycle.flow.id)).execution_paused = True
        (await db.get(OrchestrationExecution, cycle.execution.id)).handoff_receipt_ref = None
        await db.delete(await db.get(OrchestrationPullRequestBinding, cycle.binding.id))
        db.add_all(
            [
                OrchestrationDecision(
                    org_id=ORG,
                    flow_id=node.flow_id,
                    node_id=node.id,
                    kind="node_dispatched",
                    actor_id="system:orchestration-dispatch",
                    actor_kind="service",
                    actor_role="engine",
                    created_at=old_dispatch,
                    reason=json.dumps({"attempt": 1, "run_id": attempt_run_id(node.id, 1)}),
                ),
                OrchestrationDecision(
                    org_id=ORG,
                    flow_id=node.flow_id,
                    kind="flow_resumed",
                    actor_kind="human",
                    actor_id="human",
                    actor_role="owner",
                    created_at=resumed_at,
                ),
            ]
        )
        await db.commit()
    raw = {
        "pk": {"S": f"TENANT#{ORG}"},
        "sk": {"S": f"EXEC#{cycle.root}"},
        "tenant_id": {"S": ORG},
        "invocation_id": {"S": cycle.root},
        "flow_id": {"S": cycle.flow.id},
        "orchestration_node_id": {"S": cycle.node.id},
        "orchestration_node_attempt": {"N": "1"},
        "current_attempt": {"N": "1"},
        "status": {"S": "pending"},
        "persona": {"S": "developer"},
        "arrived_at": {"S": old_dispatch.isoformat()},
    }
    if case == "bound":
        raw["workload_binding"] = {"S": "running-pod"}
    if case == "registered":
        cycle.store.client.put_item(
            TableName=cycle.store.table,
            Item={
                "pk": raw["pk"],
                "sk": {"S": f"REG#{cycle.root}#1"},
            },
        )
    if case == "missing_exec":
        cycle.store.client.delete_item(TableName=cycle.store.table, Key={"pk": raw["pk"], "sk": raw["sk"]})
    else:
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    async with cycle.factory() as db:
        early = await detect_stalls(db, now, config=StallConfig(never_acknowledged_seconds=1200))
        await db.commit()
        assert early.stalls_detected == int(case != "unstarted") and early.errors == 0, early
        node = await db.get(OrchestrationNode, cycle.node.id)
        assert node.state == ("running" if case == "unstarted" else "failed")
        stalls = (
            await db.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == cycle.node.id,
                    OrchestrationDecision.kind == "node_stalled",
                )
            )
        ).all()
        assert len(stalls) == int(case != "unstarted")
        if case != "unstarted":
            assert stalls[0].reason.startswith("stalled:")
            assert await recover_developer(db, node, now=now + timedelta(seconds=120)) == "failure_requires_operator"
            assert (await db.get(OrchestrationWorkClaim, cycle.claim.id)).state == "held"
    if case != "unstarted":
        return
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}")["status"] == {"S": "pending"}
    if paused:
        return
    async with cycle.factory() as db:
        due = await detect_stalls(db, now + timedelta(seconds=1), config=StallConfig(never_acknowledged_seconds=1200))
        await db.commit()
        assert due.stalls_detected == 1 and due.errors == 0, due
        assert (await db.get(OrchestrationNode, cycle.node.id)).state == "failed"
        decision = await db.scalar(
            select(OrchestrationDecision).where(
                OrchestrationDecision.node_id == cycle.node.id,
                OrchestrationDecision.kind == "node_stalled",
            )
        )
        assert json.loads(decision.reason)["fence"] == "unacknowledged_stall"
        assert json.loads(decision.reason)["attempt"] == 1
        assert (await db.get(OrchestrationWorkClaim, cycle.claim.id)).state == "released"
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}")["status"] == {"S": "cancelled"}


@pytest.mark.parametrize(
    "contradiction,denial",
    [
        ("worker_receipt", "worker_exit_unverified"),
        ("terminal_receipt", "worker_exit_unverified"),
        ("workload_binding", "worker_exit_unverified"),
        ("exhausted", "attempts_exhausted"),
        ("changed_plan", "execution_not_settled"),
    ],
)
async def test_fenced_stall_refuses_conflicting_evidence_and_stale_allowance(cycle, monkeypatch, contradiction, denial):  # noqa: F811
    from src.orchestration.continuation import digest
    from src.orchestration.plan_lineage import execution_plan_matches
    from src.orchestration.run_reports import OrchestrationRunReport

    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: type("Writer", (), {"store": cycle.store})())
    now = datetime.now(UTC)
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        node.updated_at = now - timedelta(seconds=1200)
        (await db.get(OrchestrationExecution, cycle.execution.id)).handoff_receipt_ref = None
        await db.delete(await db.get(OrchestrationPullRequestBinding, cycle.binding.id))
        plan = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == node.flow_id))
        if contradiction == "exhausted":
            document = {
                **plan.plan_document,
                "execution_policy": {
                    **plan.plan_document["execution_policy"],
                    "limits": {
                        **plan.plan_document["execution_policy"]["limits"],
                        "max_attempts_per_node": 1,
                    },
                },
            }
            plan.plan_document, plan.plan_hash = document, digest(document)
        if contradiction == "changed_plan":
            document = {
                **plan.plan_document,
                "nodes": [
                    {"address": "cycle/E1/W1/N1", "kind": "story", "issue_ref": "43"},
                ],
                "edges": [],
            }
            plan.plan_document, plan.plan_hash = document, digest(document)
        db.add(
            OrchestrationDecision(
                org_id=ORG,
                flow_id=node.flow_id,
                node_id=node.id,
                kind="node_dispatched",
                actor_id="system:orchestration-dispatch",
                actor_kind="service",
                actor_role="engine",
                created_at=now - timedelta(seconds=1200),
                reason=json.dumps({"attempt": 1, "run_id": cycle.root}),
            )
        )
        await db.commit()
    raw = {
        "pk": {"S": f"TENANT#{ORG}"},
        "sk": {"S": f"EXEC#{cycle.root}"},
        "tenant_id": {"S": ORG},
        "invocation_id": {"S": cycle.root},
        "flow_id": {"S": cycle.flow.id},
        "orchestration_node_id": {"S": cycle.node.id},
        "orchestration_node_attempt": {"N": "1"},
        "current_attempt": {"N": "1"},
        "status": {"S": "pending"},
        "persona": {"S": "developer"},
        "arrived_at": {"S": now.isoformat()},
    }
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    async with cycle.factory() as db:
        outcome = await detect_stalls(db, now, config=StallConfig(never_acknowledged_seconds=1200))
        await db.commit()
        assert outcome.stalls_detected == 1
        assert (await db.get(OrchestrationNode, cycle.node.id)).state == "failed"
        assert (await db.get(OrchestrationWorkClaim, cycle.claim.id)).state == "released"
    fenced = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}")
    assert fenced["status"] == {"S": "cancelled"}
    if contradiction in {"worker_receipt", "terminal_receipt"}:
        async with cycle.factory() as db:
            db.add(
                OrchestrationRunReport(
                    run_id=cycle.root,
                    credential_hash="f" * 64,
                    org_id=ORG,
                    flow_id=cycle.flow.id,
                    node_id=cycle.node.id,
                    attempt=1,
                    persona="developer",
                    repo=REPO,
                    installation_id=42,
                    dispatch_metadata={},
                    worker_receipt={"recorded_at": "late"} if contradiction == "worker_receipt" else None,
                    terminal_receipt={"status": "failed"} if contradiction == "terminal_receipt" else None,
                    expires_at=now + timedelta(days=1),
                )
            )
            await db.commit()
    elif contradiction == "workload_binding":
        cycle.store.client.put_item(TableName=cycle.store.table, Item={**fenced, "workload_binding": {"S": "unexpected-pod"}})
    elif contradiction == "changed_plan":
        async with cycle.factory() as db:
            base = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == cycle.flow.id))
            amendment = OrchestrationDecision(
                org_id=ORG,
                flow_id=cycle.flow.id,
                kind="plan_amended",
                actor_kind="human",
                actor_id="human",
                actor_role="owner",
                reason="Replace the story execution definition",
            )
            db.add(amendment)
            await db.flush()
            document = {
                **base.plan_document,
                "nodes": [
                    {"address": "cycle/E1/W1/N1", "kind": "story", "issue_ref": "different"},
                ],
            }
            db.add(
                OrchestrationAcceptedPlan(
                    org_id=ORG,
                    flow_id=cycle.flow.id,
                    version=2,
                    plan_document=document,
                    plan_hash=digest(document),
                    accepted_by_decision_id=amendment.id,
                )
            )
            base.superseded_at = now
            await db.commit()
            assert not await execution_plan_matches(
                db,
                org_id=ORG,
                flow_id=cycle.flow.id,
                node_id=cycle.node.id,
                version=1,
                current_version=2,
            )
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        assert await recover_developer(db, node, now=now + timedelta(seconds=120)) == denial
        await db.commit()
        assert node.state == "failed" and node.attempts == 1
        decisions = (
            await db.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == node.id,
                    OrchestrationDecision.kind.in_(["node_stalled", "developer_retry_scheduled", "developer_retry_blocked"]),
                )
            )
        ).all()
        assert len(decisions) == 1 and decisions[0].kind == "node_stalled"
        assert json.loads(decisions[0].reason)["fence"] == "unacknowledged_stall"


async def settled(cycle, monkeypatch, failure=None, backoff=False, persona="developer"):  # noqa: F811
    observed = datetime.now(UTC) + timedelta(seconds=0 if backoff else 120)
    monkeypatch.setattr("src.orchestration.developer_recovery.utcnow", lambda: observed)
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-only-signing-key")
    async with cycle.factory() as db:
        # These scenarios fail before development hands over to review. The
        # shared review-cycle fixture otherwise seeds a committed handoff.
        (await db.get(OrchestrationExecution, cycle.execution.id)).handoff_receipt_ref = None
        db.add(
            OrchestrationDecision(
                org_id=ORG,
                flow_id=cycle.flow.id,
                node_id=cycle.node.id,
                created_at=datetime.now(UTC) - timedelta(minutes=2),
                kind="node_dispatched",
                actor_id="system:orchestration-dispatch",
                actor_kind="service",
                actor_role="engine",
                reason=json.dumps({"attempt": 1, "run_id": cycle.root}),
            )
        )
        row = await prepare_run_report(
            db,
            {
                "message_id": cycle.root,
                "tenant_id": ORG,
                "persona": persona,
                "source_ref": {"repo": REPO, "installation_id": 42, "provider_repository_id": 123},
                "orchestration": {"node_id": cycle.node.id, "flow_id": cycle.flow.id, "attempt": 1},
                "pr_binding_required": True,
            },
        )
        record_terminal(row, "failed", failure=failure)
        await db.commit()
        outcome = await observe_results(db)
        assert outcome.advanced == 1, outcome
        await db.commit()


@pytest.mark.parametrize(
    "block",
    [
        None,
        "paused",
        "exhausted",
        "expired",
        "deadline",
        "human_gate",
        "no_repair",
        "new_plan",
        "claim_changed",
        "successor",
        "uncertain_effect",
        "live_execution",
        "backoff",
        "human_failure",
        "policy_failure",
        "cancelled_failure",
    ],
)
async def test_recovery_respects_settlement_policy_and_existing_owner(cycle, monkeypatch, block):  # noqa: F811
    await settled(
        cycle,
        monkeypatch,
        {"category": "policy" if block == "policy_failure" else "cancelled"} if block in {"policy_failure", "cancelled_failure"} else None,
        backoff=block == "backoff",
    )
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        if block == "paused":
            (await db.get(OrchestrationFlow, cycle.flow.id)).execution_paused = True
        if block in {"exhausted", "expired", "deadline", "human_gate", "no_repair", "new_plan"}:
            plan = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == cycle.flow.id))
            document = json.loads(json.dumps(plan.plan_document))
            policy = document["execution_policy"]
            if block == "exhausted":
                policy["limits"]["max_attempts_per_node"] = 1
            if block == "expired":
                policy["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            if block == "deadline":
                policy["limits"]["max_wall_clock_seconds"] = 1
            if block == "human_gate":
                policy["human_gates"] = ["repair"]
            if block == "no_repair":
                policy["allowed_actions"].remove("repair")
            if block == "new_plan":
                plan.version += 1
            plan.plan_document = document
        if block == "claim_changed":
            (await db.get(OrchestrationWorkClaim, cycle.identity.claim_id)).generation += 1
        if block in {"successor", "uncertain_effect"}:
            db.add(
                OrchestrationAction(
                    org_id=ORG,
                    execution_id=cycle.execution.id,
                    operation_key="other-work",
                    kind="review_cycle_dispatch" if block == "successor" else "open_pr",
                    status="succeeded" if block == "successor" else "unknown",
                    attempt=1,
                )
            )
        if block == "live_execution":
            (await db.get(OrchestrationExecution, cycle.execution.id)).status = "runnable"
        if block == "human_failure":
            db.add(
                OrchestrationDecision(
                    org_id=ORG,
                    flow_id=cycle.flow.id,
                    node_id=node.id,
                    kind="result_observed",
                    actor_id="human",
                    actor_kind="human",
                    actor_role="owner",
                    to_state="failed",
                    reason="operator intervention",
                )
            )
        await db.commit()
        recovered = await recover_failed_developers(db)
        assert recovered == int(block is None), [
            r.reason for r in (await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "developer_retry_checked"))).all()
        ]
        assert node.state == ("ready" if block is None else "failed")
        assert node.attempts == 1
        context = await retry_context(db, node, 1)
        if block is None:
            assert context["previous_run_id"] == cycle.root
            assert context["preserve_existing_work"] is True
        else:
            assert context is None
        await db.commit()
        assert await recover_failed_developers(db) == 0


async def test_concurrent_ticks_schedule_only_one_recovery(cycle, monkeypatch):  # noqa: F811
    await settled(cycle, monkeypatch)

    async def tick():
        async with cycle.factory() as db:
            count = await recover_failed_developers(db)
            await db.commit()
            return count

    assert sum(await asyncio.gather(tick(), tick())) == 1


@pytest.mark.parametrize("source", list(NodeState))
def test_retry_proof_never_clears_other_states(source):
    result = transition(source, NodeState.READY, actor_kind=ActorKind.SERVICE, reason="retry", developer_retry_authorized=True)
    assert result.allowed == (source in {NodeState.PENDING, NodeState.FAILED})
    if source == NodeState.FAILED:
        assert not transition(source, NodeState.READY, actor_kind=ActorKind.SERVICE, reason="unverified").allowed


async def test_disabled_engine_does_not_recover(cycle, monkeypatch):  # noqa: F811
    await settled(cycle, monkeypatch)
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "false")
    async with cycle.factory() as db:
        assert await recover_failed_developers(db) == 0
        assert (await db.get(OrchestrationNode, cycle.node.id)).state == "failed"


@pytest.mark.parametrize(
    "outcome,persona",
    [("failed", "developer"), ("aborted", "developer"), ("cancelled", "developer"), ("budget_stopped", "developer"), ("failed", "reviewer")],
)
async def test_protected_recovery_requires_developer_failure(cycle, monkeypatch, outcome, persona):  # noqa: F811
    from types import SimpleNamespace
    from unittest.mock import Mock

    from sqlalchemy import delete

    from src.orchestration.run_reports import OrchestrationRunReport

    await settled(cycle, monkeypatch)
    async with cycle.factory() as db:
        await db.execute(delete(OrchestrationRunReport).where(OrchestrationRunReport.run_id == cycle.root))
        await db.commit()
        protected = Mock()
        protected._read.return_value = {
            "status": {"S": "completed"},
            "terminal_outcome": {"S": outcome},
            "persona": {"S": persona},
            "tenant_id": {"S": ORG},
            "invocation_id": {"S": cycle.root},
            "flow_id": {"S": cycle.flow.id},
            "orchestration_node_id": {"S": cycle.node.id},
            "orchestration_node_attempt": {"N": "1"},
        }
        monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: SimpleNamespace(store=protected))
        assert await recover_failed_developers(db) == int(outcome == "failed" and persona == "developer")


@pytest.mark.parametrize("expired", [False, True])
async def test_codex_developer_recovery_keeps_existing_expiry_fence(cycle, monkeypatch, expired):  # noqa: F811
    await settled(cycle, monkeypatch, persona="agent-codex-developer")
    async with cycle.factory() as db:
        if expired:
            plan = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == cycle.flow.id))
            document = dict(plan.plan_document)
            policy = dict(document["execution_policy"])
            policy["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            document["execution_policy"] = policy
            plan.plan_document = document
            await db.flush()
        assert await recover_failed_developers(db) == (0 if expired else 1)
        assert (await db.get(OrchestrationNode, cycle.node.id)).state == ("failed" if expired else "ready")
