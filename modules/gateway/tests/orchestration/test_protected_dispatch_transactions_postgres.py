"""One failed protected replay must not discard the rest of the recovery batch."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select, text, update

from src.orchestration import protected_dispatch_recovery
from src.orchestration.dispatch_pass import DispatchPassReport
from src.orchestration.models import OrchestrationDecision, OrchestrationNode
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401
from tests.orchestration.test_dispatch_pass import ORG_A, _config, _make_flow, _make_node, _make_org
from tests.orchestration.test_tick_transactions_postgres import factory as factory_fixture

factory = factory_fixture


@pytest.mark.parametrize("sqlstate", ["40P01", "22012"])
async def test_failed_replay_preserves_prior_work_and_recovers_later_nodes(factory, monkeypatch, sqlstate):
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", Mock(return_value=SimpleNamespace(store=object())))
    async with factory() as session:
        await _make_org(session)
        flow = await _make_flow(session)
        nodes = [await _make_node(session, flow, node_ref=str(i), state="running") for i in range(3)]
        for node in nodes:
            session.add(
                OrchestrationDecision(
                    org_id=flow.org_id,
                    flow_id=flow.id,
                    node_id=node.id,
                    kind="node_dispatched",
                    actor_id="engine",
                    actor_role="service",
                    actor_kind="service",
                    reason=json.dumps({"dispatch_envelope": {"message_id": node.id}}),
                )
            )
        ids = sorted(node.id for node in nodes)
        await session.commit()

    visited = []

    async def recover(session, *, node, **kwargs):
        visited.append(node.id)
        node.title = "Recovered"
        await session.flush()
        if node.id == ids[1]:
            # A real PostgreSQL error aborts the transaction, including ORM writes.
            await session.execute(text(f"DO $$ BEGIN RAISE EXCEPTION USING ERRCODE = '{sqlstate}', MESSAGE = 'replay failure'; END $$"))
        return SimpleNamespace(node_id=node.id)

    monkeypatch.setattr(protected_dispatch_recovery, "recover_one", recover)
    prior = SimpleNamespace(node_id="previously-dispatched")
    report = DispatchPassReport(pending=[prior])
    async with factory() as session:
        await session.execute(update(OrchestrationNode).where(OrchestrationNode.id == ids[0]).values(attempts=7))
        await protected_dispatch_recovery.recover_pending_protected(session, config=_config(), report=report)
        assert visited == ids
        assert report.publish_failed == 1 and not report.success
        assert report.per_org[ORG_A]["publish_failed"] == 1
        assert [p.node_id for p in report.pending] == [prior.node_id, ids[0], ids[2]]
        await session.commit()

    async with factory() as session:
        rows = {row.id: row for row in (await session.execute(select(OrchestrationNode.id, OrchestrationNode.title, OrchestrationNode.attempts)))}
        assert rows[ids[0]].title == rows[ids[2]].title == "Recovered"
        assert rows[ids[1]].title != "Recovered"
        assert rows[ids[0]].attempts == 7
