"""PMM-07: the real tick puts preparation between its commit and its publish.

`test_dispatch_preparation.py` proves `prepare_pending` behaves correctly when it
is called. This file proves the tick calls it, and calls it in the one position
where it is correct -- which is a property of the *wiring*, not of the helper.
The distinction matters more than usual here: PMM-07's inherited defect was a
capability that existed and was never reached, so a helper with a green unit test
and no caller is the exact failure being guarded against. Same reasoning as
`test_tick_tracker_projection.py`, which drives `_run()` for the same reason.

Three ordering properties are asserted directly rather than described:

- preparation happens **after** the commit, because a snapshot cannot key to rows
  that are not durable;
- preparation happens **before** the send, because the send lets a worker bind the
  execution and a bound execution refuses a snapshot;
- a rolled-back commit publishes nothing *and* provisions nothing, so the graph
  and the protected store never disagree about whether a dispatch happened.

Everything is real except the edges: moto for DynamoDB, a recording SQS double,
and the installation/repository lookups. No live AWS, no provider calls, no model
invocation.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.engine import EngineAuthorityWriter
from src.orchestration import tick_handler as tick_handler_module
from src.orchestration.models import (
    DecisionKind,
    NodeKind,
    NodeState,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.shared.models.base import Base
from src.shared.models.organization import Organization, User

ORG = "org-tick-pmm07"
REPO = "aws-e/adp"
INSTALLATION = 77_701
APPROVER = "cognito-sub-approver"
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/adp-dev-agent-submit.fifo"


@pytest.fixture
async def session_factory(monkeypatch):
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(eng.sync_engine, "begin")
    def _emit_explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("src.orchestration.tick_handler.get_session_factory", lambda: factory)
    monkeypatch.setattr("src.orchestration.tick_handler.reset_engine", lambda: None)
    yield factory
    await eng.dispose()


class RecordingSQS:
    """Records sends and, crucially, *when* they happened relative to preparation."""

    def __init__(self, journal: list[str]) -> None:
        self.calls: list[dict] = []
        self.journal = journal

    def send_message(self, **kwargs):
        self.journal.append("publish")
        self.calls.append(kwargs)
        return {"MessageId": f"msg-{len(self.calls)}"}


@pytest.fixture
def journal() -> list[str]:
    """One ordered log of commit / provision / snapshot / publish across the tick."""
    return []


@pytest.fixture(autouse=True)
def _tick_environment(monkeypatch, journal):
    from unittest.mock import AsyncMock

    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", REPO)
    monkeypatch.setenv("BG_ORCH_DISPATCH_QUEUE_URL", QUEUE_URL)
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=97_701))

    async def _resolve(_session, *, org_id):
        return INSTALLATION

    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", _resolve)

    original_commit = AsyncSession.commit

    async def recording_commit(self):
        journal.append("commit")
        return await original_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", recording_commit)


@pytest.fixture
def protected_store(monkeypatch, journal):
    """The real writer over moto, with its two blocking calls journalled."""
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        for table, pk, sk in [("authority", "pk", "sk"), ("events", "event_id", "arrived_at")]:
            ddb.create_table(
                TableName=table,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}, {"AttributeName": sk, "AttributeType": "S"}],
            )
        store = BootstrapStore(table_name="authority", dynamodb_client=ddb)
        writer = EngineAuthorityWriter(store=store, events_table="events")
        real_provision = writer.provision

        def journalling_provision(pending):
            journal.append("provision")
            return real_provision(pending)

        writer.provision = journalling_provision
        monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: writer)
        yield store, writer


@pytest.fixture(autouse=True)
def _journal_snapshot_persistence(monkeypatch, journal):
    """Record the snapshot write itself, so its position in the order is provable."""
    from src.agentauth import model_policy as model_policy_module

    original = model_policy_module._persist_snapshot

    async def recording(**kwargs):
        digest = await original(**kwargs)
        journal.append("snapshot")
        return digest

    monkeypatch.setattr(model_policy_module, "_persist_snapshot", recording)


@pytest.fixture(autouse=True)
def _journal_post_commit_flushes(monkeypatch, journal):
    """Record the flushes that follow preparation, still calling the real ones.

    These exist so "the rest of the tick kept going" is an assertion rather than a
    hope. Both flushes are downstream of `prepare_pending` on already-committed
    work, so anything that escapes preparation silently skips them -- the tick
    returns an error and the commands and projections it owed are simply never
    flushed. Wrapped rather than replaced: the real functions still run, so this
    cannot turn a broken flush green.
    """
    for name in ("flush_engine_commands", "flush_tracker_projections"):
        original = getattr(tick_handler_module, name)

        def recording(*args, _original=original, _name=name, **kwargs):
            journal.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(tick_handler_module, name, recording)


@pytest.fixture
def sqs(monkeypatch, journal):
    client = RecordingSQS(journal)
    monkeypatch.setattr("src.orchestration.dispatch_pass._get_sqs_client", lambda _region: client)
    return client


async def test_existing_execution_runs_after_controls_commit_before_new_dispatch(session_factory, protected_store, sqs, monkeypatch, journal):
    from src.orchestration.execution_runner import RunnerReport

    node_id = await _seed_ready_story(session_factory)
    journal.clear()

    async def existing_work(factory):
        # The command/observation transaction is committed, and the ready story
        # has not yet been admitted. This is the capacity-allocation boundary.
        assert journal[-1] == "commit"
        async with factory() as db:
            assert (await db.get(OrchestrationNode, node_id)).state == "ready"
        journal.append("existing-execution")
        return RunnerReport(enabled=True)

    monkeypatch.setattr(tick_handler_module, "run_execution_runner", existing_work)
    await tick_handler_module._run()
    assert journal.index("existing-execution") < journal.index("provision") < journal.index("publish")


async def _seed_ready_story(factory, *, approved: bool = True, kind: str = NodeKind.STORY.value) -> str:
    async with factory() as session:
        session.add(Organization(id=ORG, name="Tick Org", github_installation_ids=[str(INSTALLATION)]))
        flow = OrchestrationFlow(org_id=ORG, slug="pmm07", title="PMM-07 flow", intent_ref="5425", execution_paused=False)
        session.add(flow)
        await session.flush()
        if approved:
            session.add(User(id=APPROVER, org_id=ORG, team_id="team-test", email="approver@example.test", cognito_sub=f"sub:{APPROVER}"))
            await session.flush()
            session.add(
                OrchestrationDecision(
                    org_id=ORG,
                    flow_id=flow.id,
                    kind=DecisionKind.GATE_APPROVED.value,
                    actor_id=APPROVER,
                    actor_role="org_admin",
                    actor_kind="human",
                    reason="approved at the wave gate",
                )
            )
        node = OrchestrationNode(
            org_id=ORG,
            flow_id=flow.id,
            epic_ref="5425",
            wave_ref="wave-7",
            node_ref="U1",
            kind=kind,
            title="Attach a snapshot",
            state=NodeState.READY.value,
            issue_ref="5478",
        )
        session.add(node)
        await session.flush()
        await session.commit()
        return node.id


async def test_the_tick_prepares_after_its_commit_and_before_its_publish(session_factory, protected_store, sqs, journal):
    """The whole point, asserted as an order on one real tick.

    If preparation were before the commit, a snapshot would be keyed to rows that
    may never exist. If it were after the publish, a worker could already have
    bound the execution and `_persist_snapshot` would refuse with
    `dispatch_not_pending` -- the refusal the superseded gap test pinned.
    """
    store, _ = protected_store
    node_id = await _seed_ready_story(session_factory)

    report = await tick_handler_module._run()

    dispatch_report = getattr(report, "dispatch_report")
    assert dispatch_report.dispatched == 1
    assert len(sqs.calls) == 1

    # The order the tick actually executed in, with everything else filtered out.
    ordered = [step for step in journal if step in {"commit", "provision", "snapshot", "publish"}]
    assert ordered.index("commit") < ordered.index("provision") < ordered.index("snapshot") < ordered.index("publish")

    invocation_id = next(iter(dispatch_report.model_policy_receipts))
    assert dispatch_report.model_policy_receipts[invocation_id]["status"] == "available"
    execution = store._read(f"TENANT#{ORG}", f"EXEC#{invocation_id}")
    assert execution["model_policy_snapshot_digest"]["S"] == dispatch_report.model_policy_receipts[invocation_id]["snapshot_digest"]
    assert dispatch_report.success

    async with session_factory() as session:
        assert (await session.scalar(select(OrchestrationNode.state).where(OrchestrationNode.id == node_id))) == NodeState.RUNNING.value


async def test_a_rolled_back_commit_publishes_nothing_and_provisions_nothing(session_factory, protected_store, sqs, journal, monkeypatch):
    """The graph and the protected store must never disagree about a dispatch.

    `_run()` rolls back and re-raises if anything inside its transaction fails.
    Preparation sits outside that block *after* the commit, so a failed commit
    must mean no protected record and no message -- otherwise a worker could be
    handed authority for a run the graph has no row for, which is exactly the
    off-graph work `deviation.py` exists to flag.
    """
    store, _ = protected_store
    await _seed_ready_story(session_factory)

    async def failing_commit(_self):
        journal.append("commit-failed")
        raise RuntimeError("commit refused")

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)

    with pytest.raises(RuntimeError, match="commit refused"):
        await tick_handler_module._run()

    assert sqs.calls == []
    assert "provision" not in journal
    assert "snapshot" not in journal
    assert store.client.scan(TableName="authority", Select="COUNT")["Count"] == 0


async def test_an_unavailable_snapshot_does_not_stop_the_tick_dispatching(session_factory, protected_store, sqs, monkeypatch):
    """Report-only through the real tick: evidence is missing, the run still goes.

    Asserted at the tick level and not only on the helper, because this is where a
    well-meaning "fail the tick on a policy error" would be introduced. The tick
    must stay successful and the node must reach the queue.
    """
    store, _ = protected_store
    node_id = await _seed_ready_story(session_factory)

    def _unavailable(*_args, **_kwargs):
        raise RuntimeError("tenant policy source unavailable")

    monkeypatch.setattr("src.proxy.model_resolver.production_model_resolver", _unavailable)

    report = await tick_handler_module._run()

    dispatch_report = getattr(report, "dispatch_report")
    assert dispatch_report.dispatched == 1
    assert len(sqs.calls) == 1
    assert dispatch_report.errors == 0
    assert dispatch_report.publish_failed == 0
    assert dispatch_report.success
    invocation_id = next(iter(dispatch_report.model_policy_receipts))
    assert dispatch_report.model_policy_receipts[invocation_id] == {"status": "unavailable", "reason": "not_permitted"}
    assert "model_policy_snapshot" not in store._read(f"TENANT#{ORG}", f"EXEC#{invocation_id}")

    async with session_factory() as session:
        assert (await session.scalar(select(OrchestrationNode.state).where(OrchestrationNode.id == node_id))) == NodeState.RUNNING.value


async def test_a_second_tick_does_not_republish_a_started_worker(session_factory, protected_store, sqs, journal):
    """Idempotence through the real tick, which is where a retry actually happens.

    Protected worker startup, not merely a queued message, ends replay.
    """
    store, _ = protected_store
    await _seed_ready_story(session_factory)

    first = await tick_handler_module._run()
    assert len(sqs.calls) == 1
    invocation_id = next(iter(getattr(first, "dispatch_report").model_policy_receipts))
    stored = store._read(f"TENANT#{ORG}", f"EXEC#{invocation_id}")["model_policy_snapshot"]["S"]
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": f"TENANT#{ORG}"}, "sk": {"S": f"EXEC#{invocation_id}"}},
        UpdateExpression="SET #status = :active, workload_binding = :pod",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":active": {"S": "active"}, ":pod": {"S": "verified-pod"}},
    )
    journal.clear()

    second = await tick_handler_module._run()

    assert getattr(second, "dispatch_report").dispatched == 0
    assert getattr(second, "dispatch_report").model_policy_receipts == {}
    assert len(sqs.calls) == 1
    assert "provision" not in journal
    assert store._read(f"TENANT#{ORG}", f"EXEC#{invocation_id}")["model_policy_snapshot"]["S"] == stored


async def test_a_failing_provision_fails_that_dispatch_and_still_finishes_the_tick(session_factory, protected_store, sqs, journal, monkeypatch):
    """An authority write that fails must cost one dispatch, not the tick.

    Preparation runs on work that is already committed, so anything it lets escape
    is damage to correct, durable state: the flushes below it never run. The
    dispatch itself genuinely cannot proceed -- `publish_pending` would have
    refused it too -- so it is counted `publish_failed`, which forces a non-success
    report rather than a green tick that silently sent nothing.
    """
    store, writer = protected_store
    node_id = await _seed_ready_story(session_factory)

    def refusing_provision(_pending):
        journal.append("provision")
        raise RuntimeError("authority store unavailable")

    monkeypatch.setattr(writer, "provision", refusing_provision)

    report = await tick_handler_module._run()

    dispatch_report = getattr(report, "dispatch_report")
    assert "provision" in journal, "the failure never fired, so nothing was proved"
    assert dispatch_report.publish_failed == 1
    assert not dispatch_report.success
    # Dropped from `pending`, so nothing downstream can send it.
    assert dispatch_report.pending == []
    assert sqs.calls == []
    assert "snapshot" not in journal

    # The part that matters: the tick continued past the failure.
    assert journal.count("flush_engine_commands") == 1
    assert journal.count("flush_tracker_projections") == 1

    # The node stays `running` with no run -- the documented recoverable state that
    # #4211's stall detector picks up, not an off-graph execution.
    async with session_factory() as session:
        assert (await session.scalar(select(OrchestrationNode.state).where(OrchestrationNode.id == node_id))) == NodeState.RUNNING.value


async def test_an_unbuildable_authority_writer_fails_that_dispatch_and_still_finishes_the_tick(session_factory, sqs, journal, monkeypatch):
    """The same containment for *constructing* the writer, which is its own failure.

    Deliberately not using the `protected_store` fixture: that hands preparation an
    already-built writer and so cannot see this boundary at all. Here the real
    factory and the real `BootstrapStore` run with `AGENT_AUTHORITY_TABLE` absent --
    an ordinary misconfiguration or a mid-deploy env gap -- and the store refuses on
    the empty table name. The only stub is boto3's client constructor, which the
    factory evaluates before the store sees the table name; that keeps the test off
    the credential chain entirely. No request is ever issued, so there is nothing
    for moto to intercept.

    This was a real regression in the first version of this stage: the writer was
    resolved once above the loop, outside the per-node guard, so an unset table
    raised straight out of `prepare_pending` after the SQL commit and skipped both
    flushes below it. One misconfigured variable stalled the entire tick instead of
    failing the single dispatch that needed authority.
    """
    monkeypatch.delenv("AGENT_AUTHORITY_TABLE", raising=False)
    monkeypatch.setattr("src.agentauth.engine.boto3.client", lambda *_args, **_kwargs: object())
    node_id = await _seed_ready_story(session_factory)

    report = await tick_handler_module._run()

    dispatch_report = getattr(report, "dispatch_report")
    assert dispatch_report.dispatched == 1
    assert dispatch_report.publish_failed == 1
    assert not dispatch_report.success
    assert dispatch_report.pending == []
    assert sqs.calls == []
    assert dispatch_report.model_policy_receipts == {}
    assert "snapshot" not in journal

    assert journal.count("flush_engine_commands") == 1
    assert journal.count("flush_tracker_projections") == 1

    async with session_factory() as session:
        assert (await session.scalar(select(OrchestrationNode.state).where(OrchestrationNode.id == node_id))) == NodeState.RUNNING.value


async def test_an_unapproved_flow_still_dispatches_nothing_and_prepares_nothing(session_factory, protected_store, sqs):
    """Preparation must not have relaxed an unrelated gate.

    Without an approval row `resolve_engine_genesis` refuses, so there is no
    dispatch to prepare. Worth asserting explicitly: the new stage runs on
    `report.pending`, and a version of it that provisioned anything for a refused
    node would be creating authority for work no human approved.
    """
    store, _ = protected_store
    await _seed_ready_story(session_factory, approved=False)

    report = await tick_handler_module._run()

    dispatch_report = getattr(report, "dispatch_report")
    assert dispatch_report.dispatched == 0
    assert dispatch_report.genesis_refused == 1
    assert dispatch_report.model_policy_receipts == {}
    assert sqs.calls == []
    assert store.client.scan(TableName="authority", Select="COUNT")["Count"] == 0
