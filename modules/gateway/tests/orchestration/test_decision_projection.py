"""Current-decision graph bounds and unchanged audit/tenant contracts."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

import pytest
from sqlalchemy import event, insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration.decision_projection import projection_sql
from src.orchestration.models import OrchestrationDecision
from src.orchestration.repository import OrchestrationRepository
from src.shared.models.base import Base
from tests.migrations import conftest_postgres as postgres
from tests.orchestration import test_list_flows as fixtures
from tests.orchestration.decision_projection_fixture import apply_migration, migration
from tests.orchestration.test_flow_read_performance import client_for

pg_server = postgres.pg_server
pg_url = postgres.pg_url
app_with_router = fixtures.app_with_router


@pytest.mark.parametrize("dispatch_only", [False, True])
def test_sqlite_projection_in_fresh_process(tmp_path, dispatch_only):
    program = dedent('''
        import asyncio
        import json
        import sys

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from src.orchestration.repository import OrchestrationRepository

        async def main():
            engine = create_async_engine("sqlite+aiosqlite:///" + sys.argv[1], poolclass=NullPool)
            try:
                async with engine.begin() as connection:
                    await connection.execute(text("""
                        CREATE TABLE orchestration_decisions (
                            id TEXT PRIMARY KEY, org_id TEXT, flow_id TEXT, node_id TEXT,
                            kind TEXT, actor_id TEXT, actor_kind TEXT, reason TEXT,
                            rejection_reason TEXT, created_at TEXT
                        )
                    """))
                    await connection.execute(text("""
                        INSERT INTO orchestration_decisions
                            (id, org_id, flow_id, node_id, kind, reason, created_at)
                        VALUES (:id, :org_id, 'flow', 'node', :kind, :reason, '2026-01-01 00:00:00')
                    """), [
                        dict(id="a-dispatch", org_id="tenant", kind="node_dispatched", reason='{"attempt":1}'),
                        dict(id="b-stall", org_id="tenant", kind="node_stalled", reason=None),
                        dict(id="c-result", org_id="tenant", kind="result_observed", reason='{"attempt":1}'),
                        dict(id="z-clearer", org_id="tenant", kind="node_dispatched", reason="malformed"),
                        dict(id="zz-foreign", org_id="foreign", kind="node_dispatched", reason='{}'),
                    ])
                dispatch_only = json.loads(sys.argv[2])
                expected = ["a-dispatch"] if dispatch_only else ["a-dispatch", "b-stall", "c-result", "z-clearer"]
                for read_number in range(2):
                    async with async_sessionmaker(engine)() as session:
                        decisions = await OrchestrationRepository(session).display_decisions(
                            org_id="tenant", flow_id="flow", node_ids=["node"], dispatch_only=dispatch_only
                        )
                        assert [decision.id for decision in decisions] == expected, read_number
                        repeated = await OrchestrationRepository(session).display_decisions(
                            org_id="tenant", flow_id="flow", node_ids=["node"], dispatch_only=dispatch_only
                        )
                        assert repeated == decisions
            finally:
                await engine.dispose()

        asyncio.run(main())
    ''')
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "decisions.db"), json.dumps(dispatch_only)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(params=["sqlite", "postgresql"])
async def session(request):
    if request.param == "sqlite":
        async for database in fixtures.session.__wrapped__():
            yield database
        return
    try:
        url = request.getfixturevalue("pg_url")
    except pytest.skip.Exception as exc:
        pytest.fail(f"PostgreSQL 16 is required for decision projection verification: {exc}")
    engine = create_async_engine(postgres.to_async_url(url), connect_args={"server_settings": {"statement_timeout": "30000"}})
    try:
        async with engine.connect() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.commit()
            await connection.run_sync(apply_migration)
        async with async_sessionmaker(engine, expire_on_commit=False)() as database:
            yield database
    finally:
        await engine.dispose()


async def test_graph_never_reads_full_ledger(session, app_with_router, monkeypatch, record_property):
    flow = await fixtures.seed_flow(session, slug="projection-regression")
    node = await fixtures.seed_node(session, flow, node_ref="story", state="failed")
    node.attempts = 1
    for start in range(0, 65000, 1000):
        await session.execute(
            insert(OrchestrationDecision),
            [
                dict(
                    id=f"{number:036d}",
                    org_id=flow.org_id,
                    flow_id=flow.id,
                    node_id=node.id,
                    kind={0: "node_dispatched", 1: "node_stalled"}.get(number, "transition_rejected"),
                    reason='{"attempt":1,"run_id":"current"}' if number == 0 else None,
                    actor_id="unrelated",
                    actor_role="service",
                    actor_kind="service",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
                for number in range(start, start + 1000)
            ],
        )
    await session.commit()

    parameters = {"org_id": flow.org_id, "flow_id": flow.id, "node_ids": json.dumps([node.id])}
    if session.get_bind().dialect.name == "postgresql":
        plans = {}
        for indexed in (True, False):
            if not indexed:
                for name in {**migration("104_decision_display_projection").INDEXES, **migration().INDEXES}:
                    await session.execute(text(f'DROP INDEX "{name}"'))
            await session.execute(text("ANALYZE orchestration_decisions"))
            plan = (await session.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + projection_sql()), parameters)).scalar_one()
            plans[str(indexed)] = plan
        await session.commit()
        async with session.bind.connect() as connection:
            await connection.run_sync(apply_migration)
        indexed_plan = plans["True"][0]["Plan"]
        unindexed_plan = plans["False"][0]["Plan"]
        assert indexed_plan["Shared Hit Blocks"] < unindexed_plan["Shared Hit Blocks"] / 10
        assert "ix_decision_display_category" in json.dumps(indexed_plan)
        record_property("projection_plans", json.dumps(plans))
        print("projection_buffers", indexed_plan["Shared Hit Blocks"], unindexed_plan["Shared Hit Blocks"])

    statements = []
    selected_counts = []
    original = OrchestrationRepository.display_decisions

    async def count_selected(repository, **kwargs):
        decisions = await original(repository, **kwargs)
        selected_counts.append(len(decisions))
        return decisions

    def trace(_connection, _cursor, statement, _parameters, _context, _many):
        if "adp_decision_category_v2" in statement:
            statements.append(statement)

    async def forbidden_ledger(*args, **kwargs):
        pytest.fail("graph materialized the full decision ledger")

    monkeypatch.setattr(OrchestrationRepository, "list_decisions", forbidden_ledger)
    monkeypatch.setattr(OrchestrationRepository, "display_decisions", count_selected)
    event.listen(session.bind.sync_engine, "before_cursor_execute", trace)
    try:
        async with client_for(app_with_router) as client:
            response = await client.get(f"/orchestration/flows/{flow.id}?include_activity=false")
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", trace)
    assert response.status_code == 200, response.text
    assert len(response.json()["nodes"]) == 1
    assert response.json()["nodes"][0]["stalled"] is True
    assert response.json()["nodes"][0]["run_id"] == "current"
    assert len(statements) == 1
    assert selected_counts == [2]


async def test_decision_scope_order_and_audit(session, app_with_router):
    flow = await fixtures.seed_flow(session, slug="projection-order")
    node = await fixtures.seed_node(session, flow, node_ref="story", state="running")
    node.attempts = 2
    rows = [
        ("01", "node_dispatched", '{"attempt":2,"run_id":"must-not-resurrect"}'),
        ("02", "node_dispatched", '{"attempt":1,"run_id":"wrong-attempt"}'),
        ("03", "node_dispatched", "[]"),
        ("04", "result_observed", '{"attempt":2,"evidence":"must-not-resurrect"}'),
        ("05", "result_observed", '{"attempt":1,"evidence":"wrong-attempt"}'),
        ("06", "result_observed", '{"evidence":"no-attempt"}'),
        ("07", "gate_approved", "yes"),
        ("08", "gate_rejected", "later"),
        ("09", "node_stalled", None),
        ("10", "node_halted", None),
    ]
    for identity, kind, reason in reversed(rows):
        session.add(
            OrchestrationDecision(
                id=identity,
                org_id=flow.org_id,
                flow_id=flow.id,
                node_id=node.id,
                kind=kind,
                actor_id="test",
                actor_role="service",
                actor_kind="service",
                reason=reason,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    await session.commit()
    repository = OrchestrationRepository(session)
    selected = await repository.display_decisions(org_id=flow.org_id, flow_id=flow.id, node_ids=[node.id])
    assert [row.id for row in selected] == ["02", "03", "05", "06", "08", "10"]
    assert await repository.display_decisions(org_id="foreign", flow_id=flow.id, node_ids=[node.id]) == []
    assert await repository.display_decisions(org_id=flow.org_id, flow_id="missing", node_ids=[node.id]) == []
    assert len(await repository.list_decisions(org_id=flow.org_id, flow_id=flow.id)) == len(rows)
    async with client_for(app_with_router) as client:
        response = await client.get(f"/orchestration/flows/{flow.id}?include_activity=false")
        missing = await client.get("/orchestration/flows/missing?include_activity=false")
        fixtures.client_for(app_with_router, org_id=fixtures.ORG_B).close()
        foreign = await client.get(f"/orchestration/flows/{flow.id}?include_activity=false")
    assert response.status_code == 200, response.text
    assert missing.status_code == foreign.status_code == 404
    summary = response.json()["nodes"][0]
    assert summary["run_id"] is None
    assert summary["result_summary"] is None


@pytest.mark.parametrize("node_count", [1, 20])
async def test_all_categories_bound_each_node(session, node_count):
    flow = await fixtures.seed_flow(session, slug="projection-categories")
    nodes = [await fixtures.seed_node(session, flow, node_ref=f"story-{number}") for number in range(node_count)]
    events = [
        ("node_stalled", "broken", "other", "service", None),
        ("node_dispatched", '{"attempt":1}', "other", "service", None),
        ("node_dispatched", "broken", "other", "service", None),
        ("result_observed", '{"attempt":true}', "other", "service", None),
        ("result_observed", "{}", "other", "service", None),
        ("gate_rejected", "verbatim", "other", "service", None),
        ("evaluation_waived", '{"reason":null,"criterion_ids":[],"plan_version":1}', "human", "human", None),
        ("evaluation_waived", "{}", "human", "human", None),
        ("transition_rejected", None, "system:orchestration-dispatch", "service", '{"contract":"dispatch-admission-refusal/v1"}'),
        ("evaluation_evidence_wait", "{}", "system:orchestration-tick", "service", None),
        ("evaluation_evidence_ready", "broken", "system:orchestration-tick", "service", None),
        ("developer_retry_checked", "{}", "system:developer-recovery", "service", None),
        ("transition_rejected", None, "system:repository-evaluation", "service", '{"action":"repository_evaluation_blocked"}'),
        ("result_observed", '{"action":"repository_evaluation"}', "system:repository-evaluation", "service", None),
    ]
    await session.execute(
        insert(OrchestrationDecision),
        [
            dict(
                id=f"{node_number:016d}-{event_number:016d}",
                org_id=flow.org_id,
                flow_id=flow.id,
                node_id=node.id,
                kind=kind,
                actor_id=actor,
                actor_role=actor_kind,
                actor_kind=actor_kind,
                reason=reason,
                rejection_reason=rejection,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            for node_number, node in enumerate(nodes)
            for event_number, (kind, reason, actor, actor_kind, rejection) in enumerate(events)
        ],
    )
    selected = await OrchestrationRepository(session).display_decisions(org_id=flow.org_id, flow_id=flow.id, node_ids=[node.id for node in nodes])
    assert len(selected) == 13 * node_count
    for node_number, node in enumerate(nodes):
        assert {row.id for row in selected if row.node_id == node.id} == {
            f"{node_number:016d}-{event_number:016d}" for event_number in range(14) if event_number != 7
        }


async def test_graph_clearers_waiver_and_independent_observation(session, app_with_router, monkeypatch):
    from src.orchestration import routes

    flow = await fixtures.seed_flow(session, slug="projection-clearers")
    node = await fixtures.seed_node(session, flow, node_ref="eval", state="waived", kind="eval")
    node.attempts = 1
    events = [
        ("node_dispatched", '{"attempt":1,"run_id":"retained"}', "other", "service", None),
        ("transition_rejected", None, "system:orchestration-dispatch", "service", '{"contract":"dispatch-admission-refusal/v1"}'),
        ("node_dispatched", "broken", "other", "service", None),
        ("evaluation_evidence_wait", "{}", "system:orchestration-tick", "service", None),
        ("evaluation_evidence_ready", "broken", "system:orchestration-tick", "service", None),
        ("result_observed", '{"attempt":true}', "other", "service", None),
        ("result_observed", '{"attempt":1.0,"evidence":"new"}', "other", "service", None),
        ("evaluation_waived", '{"reason":"ok","criterion_ids":[],"plan_version":1}', "human", "human", None),
        ("evaluation_waived", "{}", "human", "human", None),
        ("developer_retry_checked", '{"attempt":1,"result":"retry"}', "system:developer-recovery", "service", None),
        ("developer_retry_checked", "{}", "wrong", "service", None),
        ("gate_approved", "yes", "human", "human", None),
        ("gate_rejected", "no", "human", "human", None),
    ]
    for number, (kind, reason, actor, actor_kind, rejection) in enumerate(events):
        session.add(
            OrchestrationDecision(
                id=f"{number:036d}",
                org_id=flow.org_id,
                flow_id=flow.id,
                node_id=node.id,
                kind=kind,
                reason=reason,
                actor_id=actor,
                actor_role=actor_kind,
                actor_kind=actor_kind,
                rejection_reason=rejection,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    await session.commit()
    inputs = []
    original = routes.node_progress

    def observe(**kwargs):
        inputs.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(routes, "node_progress", observe)
    async with client_for(app_with_router) as client:
        response = await client.get(f"/orchestration/flows/{flow.id}?include_activity=false")
    assert response.status_code == 200, response.text
    summary = response.json()["nodes"][0]
    assert summary["run_id"] == "retained"
    assert summary["result_summary"] == "new"
    assert summary["evaluation_waiver"]["reason"] == "ok"
    assert summary["last_gate_decision"]["action"] == "changes_requested"
    assert len(inputs) == 1
    assert inputs[0]["admission_refusal"] is None
    assert inputs[0]["evaluation_wait"] is None
    assert inputs[0]["developer_retry"] == {"attempt": 1, "result": "retry"}
    assert inputs[0]["observed_at"].startswith("2026-01-01T00:00:00")


async def test_graph_displays_external_completion_and_retains_worker_run(session, app_with_router):
    flow = await fixtures.seed_flow(session, slug="external-completion")
    node = await fixtures.seed_node(session, flow, node_ref="release", state="passed", kind="story")
    node.attempts = 4
    summary = "Completed externally: lifecycle, denial and recovery passed. https://example.invalid/evidence"
    for number, (kind, actor_kind, payload) in enumerate(
        [
            ("node_dispatched", "service", {"attempt": 4, "run_id": "original-worker"}),
            ("result_observed", "service", {"attempt": 4, "evidence": "Worker exhausted turn budget"}),
            (
                "external_completion_accepted",
                "human",
                {
                    "contract": "external-operations-completion/v1",
                    "request": {"expected_attempt": 4},
                    "summary": summary,
                },
            ),
        ]
    ):
        session.add(
            OrchestrationDecision(
                id=f"{number:036d}",
                org_id=flow.org_id,
                flow_id=flow.id,
                node_id=node.id,
                kind=kind,
                actor_id="owner" if actor_kind == "human" else "engine",
                actor_role=actor_kind,
                actor_kind=actor_kind,
                reason=json.dumps(payload),
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    await session.commit()
    async with client_for(app_with_router) as client:
        response = await client.get(f"/orchestration/flows/{flow.id}?include_activity=false")
    assert response.status_code == 200, response.text
    result = response.json()["nodes"][0]
    assert result["state"] == "passed"
    assert result["result_summary"] == summary
    assert result["delivery_progress"]["detail"] == summary
    assert result["run_id"] == "original-worker"
