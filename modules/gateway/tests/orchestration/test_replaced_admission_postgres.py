"""Independent PostgreSQL sessions exercise recovery races and atomic rollback."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.execution_state import ExecutionIdentity, ExecutionPhase, ExecutionStatus, PhaseAdvance
from src.orchestration.execution_store import advance_execution, create_execution
from src.orchestration.models import (
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.replaced_admission import recover_replaced_admissions
from tests.orchestration.test_execution_store_postgres import (
    CLAIM,
    ORG_A,
    PLAN_VERSION,
)
from tests.orchestration.test_execution_store_postgres import graph as graph_fixture
from tests.orchestration.test_execution_store_postgres import pg_engine as pg_engine_fixture
from tests.orchestration.test_execution_store_postgres import pg_server as pg_server_fixture
from tests.orchestration.test_execution_store_postgres import pg_session_factory as pg_factory_fixture
from tests.orchestration.test_execution_store_postgres import pg_url as pg_url_fixture

graph = graph_fixture
pg_engine = pg_engine_fixture
pg_server = pg_server_fixture
pg_session_factory = pg_factory_fixture
pg_url = pg_url_fixture

pytestmark = pytest.mark.integration


async def decision_history(session):
    rows = await session.scalars(select(OrchestrationDecision).order_by(OrchestrationDecision.id))
    return [(row.id, row.kind, row.reason) for row in rows]


@pytest.fixture
async def replacement(pg_session_factory, graph):  # noqa: F811
    async with pg_session_factory() as session:
        node = await session.get(OrchestrationNode, graph["node_a"])
        node.attempts, node.state, node.issue_ref = 2, "passed", "5142"
        old_identity = ExecutionIdentity(ORG_A, node.id, 1, PLAN_VERSION, CLAIM, 1)
        old = await create_execution(session, identity=old_identity, flow_id=node.flow_id)
        claim = await session.get(OrchestrationWorkClaim, CLAIM)
        claim.generation = 2
        new_identity = ExecutionIdentity(ORG_A, node.id, 2, PLAN_VERSION, CLAIM, 2)
        new = await create_execution(session, identity=new_identity, flow_id=node.flow_id)
        await advance_execution(
            session,
            identity=new_identity,
            advance=PhaseAdvance(phase=ExecutionPhase.CONCLUDED, status=ExecutionStatus.CONCLUDED, expected_revision=new.record.revision),
        )
        claim.state, claim.release_reason, claim.active_run_id = "released", "completed", None
        claim.claim_event_id = attempt_run_id(node.id, 2)
        await session.commit()
        raw = {
            "tenant_id": {"S": ORG_A},
            "invocation_id": {"S": attempt_run_id(node.id, 1)},
            "flow_id": {"S": node.flow_id},
            "orchestration_node_id": {"S": node.id},
            "orchestration_node_attempt": {"N": "1"},
            "status": {"S": "completed"},
            "terminal_outcome": {"S": "failed"},
        }
        return {
            "old": old.record.id,
            "new": new.record.id,
            "node": node.id,
            "flow": node.flow_id,
            "raw": raw,
            "decisions": await decision_history(session),
        }


async def test_concurrent_recovery_records_one_retirement(pg_session_factory, replacement):  # noqa: F811
    barrier = threading.Barrier(2)

    def read(*args):
        barrier.wait(timeout=5)
        return replacement["raw"]

    store = SimpleNamespace(_read=read)
    results = await asyncio.wait_for(
        asyncio.gather(
            recover_replaced_admissions(pg_session_factory, store=store),
            recover_replaced_admissions(pg_session_factory, store=store),
        ),
        10,
    )
    assert sorted(count for count, _ in results) == [0, 1]
    async with pg_session_factory() as session:
        old = await session.get(OrchestrationExecution, replacement["old"])
        assert old.status == "superseded" and old.claim_generation == 1 and old.attempts == 0
        decisions = await decision_history(session)
        initial_ids = {row[0] for row in replacement["decisions"]}
        assert [row for row in decisions if row[0] in initial_ids] == replacement["decisions"]
        created = [row for row in decisions if row[0] not in initial_ids]
        assert len(created) == 1 and created[0][1] == "replaced_admission_settled"


@pytest.mark.parametrize("model,key", [(OrchestrationFlow, "flow"), (OrchestrationNode, "node")])
async def test_busy_dispatch_locks_are_skipped_then_retried(pg_session_factory, replacement, model, key):  # noqa: F811
    store = SimpleNamespace(_read=Mock(return_value=replacement["raw"]))
    async with pg_session_factory() as dispatcher:
        await dispatcher.scalar(select(model).where(model.id == replacement[key]).with_for_update())
        assert await asyncio.wait_for(recover_replaced_admissions(pg_session_factory, store=store), 2) == (0, "")
        await dispatcher.rollback()
    assert await recover_replaced_admissions(pg_session_factory, store=store) == (1, "")


@pytest.mark.parametrize("change", ["action", "successor_revision", "claim_owner"])
async def test_change_after_snapshot_prevents_retirement(pg_session_factory, replacement, change):  # noqa: F811
    entered, release = threading.Event(), threading.Event()

    def read(*args):
        entered.set()
        assert release.wait(timeout=5)
        return replacement["raw"]

    recovery = asyncio.create_task(recover_replaced_admissions(pg_session_factory, store=SimpleNamespace(_read=read)))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        async with pg_session_factory() as mutator:
            if change == "action":
                mutator.add(
                    OrchestrationAction(
                        org_id=ORG_A,
                        execution_id=replacement["old"],
                        operation_key="effect-before-settlement",
                        kind="merge_pull_request",
                        status="unknown",
                    )
                )
            elif change == "successor_revision":
                successor = await mutator.get(OrchestrationExecution, replacement["new"])
                successor.revision += 1
            else:
                claim = await mutator.get(OrchestrationWorkClaim, CLAIM)
                claim.owner_ref = "another-owner"
            await mutator.commit()
        release.set()
        assert await asyncio.wait_for(recovery, 5) == (0, "")
        async with pg_session_factory() as observer:
            assert (await observer.get(OrchestrationExecution, replacement["old"])).status == "runnable"
            assert await decision_history(observer) == replacement["decisions"]
    finally:
        release.set()
        await recovery


async def test_commit_failure_rolls_back_retirement_and_audit(pg_session_factory, replacement, monkeypatch):  # noqa: F811
    original = AsyncSession.commit

    async def failed_commit(self):
        raise RuntimeError("injected commit failure")

    store = SimpleNamespace(_read=Mock(return_value=replacement["raw"]))
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", failed_commit)
        assert await recover_replaced_admissions(pg_session_factory, store=store) == (0, "")
    assert AsyncSession.commit is original
    async with pg_session_factory() as observer:
        assert (await observer.get(OrchestrationExecution, replacement["old"])).status == "runnable"
        assert await decision_history(observer) == replacement["decisions"]
    assert await recover_replaced_admissions(pg_session_factory, store=store) == (1, "")
