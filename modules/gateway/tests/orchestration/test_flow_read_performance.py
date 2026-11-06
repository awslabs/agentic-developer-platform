"""Populated PostgreSQL 16 and request-wide lineage regressions for flow reads."""

# ruff: noqa: F811 -- imported pytest fixtures are requested by the tests

import hashlib
import json
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import event, func, insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration import plan_lineage
from src.orchestration.amend import amend_plan
from src.orchestration.cost import CostStatus, get_flow_cost
from src.orchestration.models import (
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import ActorKind
from src.shared.models.base import Base
from src.shared.models.usage import UsageLog
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.orchestration import test_pending_amendments as fixtures
from tests.orchestration import test_read_api as activity_fixtures
from tests.orchestration.decision_projection_fixture import apply_migration
from tests.orchestration.flow_read_fixture import populate_flows
from tests.orchestration.legacy_plan import persist_legacy_plan
from tests.orchestration.test_list_flows import app_with_router, seed_flow, seed_node, session  # noqa: F401
from tests.orchestration.test_list_flows import client_for as sync_client_for
from tests.orchestration.test_progress_projection import execution


@pytest.fixture(autouse=True)
async def scoped_activity_reader(monkeypatch):
    from src.orchestration import activity_reads

    reader = activity_reads.ActivityReader(cache_enabled=False)
    monkeypatch.setattr(activity_reads, "shared_activity_reader", lambda: reader)
    yield reader
    await reader.close()


@pytest.mark.parametrize(
    ("reason", "object_value", "waiver_keys", "integer_attempt"),
    [
        (None, True, False, False),
        ("", True, False, False),
        ("null", False, False, False),
        ("true", False, False, False),
        ("0", False, False, False),
        ('"text"', False, False, False),
        ("[]", False, False, False),
        ('{"attempt": 1', False, False, False),
        ('{"attempt": 01}', False, False, False),
        ('{"reason": "x", "criterion_ids": [], "plan_version": 2}', True, True, False),
        ('{"reason": "x", "criterion_ids": [], "plan_version": 2, "reason": "y"}', True, True, False),
        ('{"reason": "x", "criterion_ids": []}', True, False, False),
        ('{"attempt": true}', True, False, True),
        ('{"attempt": 1.0}', True, False, False),
        ('{"attempt": 1234567890123456789012345678901234567890}', True, False, True),
        ('{"attempt": 1e400}', True, False, False),
        ('{"attempt": NaN}', True, False, False),
        ('{"attempt": Infinity}', True, False, False),
        ('{"attempt": -Infinity}', True, False, False),
        ('{"reason": "NaN", "criterion_ids": [], "plan_version": 2}', True, True, False),
    ],
)
def test_decision_payload_python_qualification_oracle(reason, object_value, waiver_keys, integer_attempt):
    try:
        value = json.loads(reason or "{}")
    except (ValueError, TypeError):
        value = None
    assert isinstance(value, dict) is object_value
    assert (isinstance(value, dict) and all(key in value for key in ("reason", "criterion_ids", "plan_version"))) is waiver_keys
    assert (isinstance(value, dict) and isinstance(value.get("attempt"), int)) is integer_attempt


@asynccontextmanager
async def client_for(app):
    """Keep asyncpg database work on the fixture's event loop."""
    # Reuse the established auth overrides without starting TestClient's portal.
    sync_client_for(app).close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


def request_statement_recorder(session):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(session.bind.sync_engine, "before_cursor_execute", record)

    def stop():
        event.remove(session.bind.sync_engine, "before_cursor_execute", record)

    return statements, stop


def lineage_statements(statements):
    return [
        sql
        for sql in statements
        if (
            "FROM orchestration_executions JOIN orchestration_accepted_plans" in sql
            or "FROM orchestration_accepted_plans LEFT OUTER JOIN orchestration_decisions" in sql
            or "FROM orchestration_flows" in sql
            and "orchestration_flows.slug" in sql
            and "orchestration_flows.id IN" in sql
        )
    ]


async def test_graph_loads_current_plan_document_once(session, app_with_router):
    compiled = await persist_legacy_plan(session, fixtures.base_proposal(), fixtures.approval())
    await session.commit()
    statements, stop = request_statement_recorder(session)
    try:
        async with client_for(app_with_router) as client:
            response = await client.get(f"/orchestration/flows/{compiled.flow_id}?include_activity=false")
    finally:
        stop()
    assert response.status_code == 200, response.text
    full_plan_reads = [
        sql
        for sql in statements
        if "FROM orchestration_accepted_plans" in sql and "orchestration_accepted_plans.plan_document" in sql.split("FROM")[0]
    ]
    assert len(full_plan_reads) == 1, full_plan_reads
    assert any(sql.startswith("SELECT EXISTS") for sql in statements)


async def test_graph_preserves_empty_metadata_for_a_legacy_null_plan(session, app_with_router):
    flow = await seed_flow(session, slug="legacy-null-plan")
    await seed_node(session, flow, node_ref="pending-story")
    session.add(OrchestrationAcceptedPlan(org_id=flow.org_id, flow_id=flow.id, plan_document=None, plan_hash="legacy-null"))
    await session.commit()
    async with client_for(app_with_router) as client:
        response = await client.get(f"/orchestration/flows/{flow.id}?include_activity=false")
    assert response.status_code == 200, response.text
    assert response.json()["wave_metadata"] == response.json()["epic_metadata"] == []
    assert response.json()["execution_policy"] is None


async def test_bounded_lineage_queries_cover_rows_chips_waves_and_graph(session, app_with_router):
    proposal = fixtures.base_proposal()
    compiled = await persist_legacy_plan(session, proposal, fixtures.approval())
    story = await session.scalar(
        select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "story-a")
    )
    story.state, story.attempts = "running", 1
    await session.commit()
    await execution(session, story, accepted_plan_version=1, status="blocked", block_code="human_input_required")
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    await session.commit()

    for flow_index in range(19):
        flow = await seed_flow(session, slug=f"owned-flow-{flow_index:02d}")
        for node_index in range(100):
            await seed_node(session, flow, node_ref=f"node-{node_index:03d}")
    await session.commit()

    statements, stop = request_statement_recorder(session)
    try:
        async with client_for(app_with_router) as client:
            listing = await client.get("/orchestration/flows")
            list_lineage = lineage_statements(statements)
            graph = await client.get(f"/orchestration/flows/{compiled.flow_id}")
    finally:
        stop()
    assert listing.status_code == 200, listing.text
    assert graph.status_code == 200, graph.text
    body = listing.json()
    assert body["total"] == 20
    amended = next(flow for flow in body["flows"] if flow["id"] == compiled.flow_id)
    assert amended["display_counts"]["stalled"] == 1
    assert amended["waves"] and any(wave["display_counts"]["stalled"] == 1 for wave in amended["waves"])
    assert body["status_counts"]["attention_needed"] == 1
    assert sum(body["status_counts"].values()) == 20
    graph_story = next(node for node in graph.json()["nodes"] if node["id"] == story.id)
    assert graph_story["display_state"] == "stalled"
    graph_lineage = lineage_statements(statements)[len(list_lineage) :]
    assert 0 < len(list_lineage) <= 8, list_lineage
    assert 0 < len(graph_lineage) <= 8, graph_lineage


@pytest.mark.parametrize("corruption", ["missing_receipt", "foreign_receipt", "missing_predecessor"])
async def test_lineage_refusals_do_not_grant_stalled_state(session, app_with_router, corruption):
    compiled = await persist_legacy_plan(session, fixtures.base_proposal(), fixtures.approval())
    story = await session.scalar(
        select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "story-a")
    )
    evaluation = await session.scalar(
        select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "eval")
    )
    story.state, story.attempts = "running", 1
    await session.commit()
    await execution(session, story, accepted_plan_version=1, status="blocked", block_code="human_input_required")
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    await session.commit()
    evaluation.state, evaluation.attempts = "running", 1
    await execution(session, evaluation, accepted_plan_version=1, status="blocked", block_code="human_input_required")
    await session.commit()

    story_id, evaluation_id = story.id, evaluation.id
    async with client_for(app_with_router) as client:
        valid = await client.get(f"/orchestration/flows/{compiled.flow_id}")
        assert valid.status_code == 200, valid.text
        states = {node["id"]: node["display_state"] for node in valid.json()["nodes"]}
        assert states[story_id] == "stalled"
        assert states[evaluation_id] != "stalled"

        plans = (await session.scalars(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == compiled.flow_id))).all()
        current = next(plan for plan in plans if plan.superseded_at is None)
        if corruption == "missing_predecessor":
            await session.delete(next(plan for plan in plans if plan.version == 1))
        elif corruption == "missing_receipt":
            current.accepted_by_decision_id = "missing-decision"
        else:
            receipt = OrchestrationDecision(
                org_id="foreign-tenant",
                flow_id=compiled.flow_id,
                kind="plan_amended",
                actor_id="someone",
                actor_role="org_admin",
                actor_kind="human",
                reason="foreign receipt",
            )
            session.add(receipt)
            await session.flush()
            current.accepted_by_decision_id = receipt.id
        await session.commit()
        session.expire_all()
        refused = await client.get(f"/orchestration/flows/{compiled.flow_id}")
        assert refused.status_code == 200, refused.text
        states = {node["id"]: node["display_state"] for node in refused.json()["nodes"]}
        assert states[story_id] != "stalled"
        assert states[evaluation_id] != "stalled"
        listing = await client.get("/orchestration/flows")
        assert listing.status_code == 200, listing.text
        assert listing.json()["flows"][0]["display_counts"]["stalled"] == 0
        assert (await client.get(f"/orchestration/flows/{'foreign-flow-id'}")).status_code == 404


async def test_lineage_refuses_101_hops_but_not_99_or_100(session, app_with_router):
    compiled = await persist_legacy_plan(session, fixtures.base_proposal(), fixtures.approval())
    story = await session.scalar(
        select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "story-a")
    )
    story.state, story.attempts = "running", 1
    await session.commit()
    await execution(session, story, accepted_plan_version=1, status="blocked", block_code="human_input_required")
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    await session.commit()
    current = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.flow_id == compiled.flow_id, OrchestrationAcceptedPlan.superseded_at.is_(None)
        )
    )

    async with client_for(app_with_router) as client:
        for version in range(3, 103):
            current.superseded_at = datetime.now(UTC)
            decision = OrchestrationDecision(
                org_id=fixtures.ORG_A,
                flow_id=compiled.flow_id,
                kind="plan_amended",
                actor_id="owner",
                actor_role="org_admin",
                actor_kind="human",
                reason="same definition",
            )
            session.add(decision)
            await session.flush()
            next_plan = OrchestrationAcceptedPlan(
                org_id=fixtures.ORG_A,
                flow_id=compiled.flow_id,
                version=version,
                plan_document=current.plan_document,
                plan_hash=current.plan_hash,
                accepted_by_decision_id=decision.id,
            )
            session.add(next_plan)
            await session.commit()
            current = next_plan
            if version in (100, 101, 102):
                response = await client.get(f"/orchestration/flows/{compiled.flow_id}")
                assert response.status_code == 200, response.text
                actual = next(node for node in response.json()["nodes"] if node["id"] == story.id)
                assert (actual["display_state"] == "stalled") is (version <= 101)


async def test_candidate_overflow_does_not_poison_smaller_page_waves(session, app_with_router):
    compiled = await persist_legacy_plan(session, fixtures.base_proposal(), fixtures.approval())
    story = await session.scalar(
        select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "story-a")
    )
    story.state, story.attempts = "running", 1
    await session.commit()
    await execution(session, story, accepted_plan_version=1, status="blocked", block_code="human_input_required")
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    await session.commit()

    overflow_flow = await seed_flow(session, slug="candidate-overflow")
    session.add_all(
        [
            OrchestrationAcceptedPlan(
                org_id=fixtures.ORG_A,
                flow_id=overflow_flow.id,
                version=version,
                plan_document={},
                plan_hash="invalid",
                superseded_at=datetime.now(UTC) if version == 1 else None,
            )
            for version in (1, 2)
        ]
    )
    await session.commit()

    async with client_for(app_with_router) as client:
        for node_index in range(1000):
            node = await seed_node(session, overflow_flow, node_ref=f"overflow-{node_index}", state="running")
            node.attempts = 1
            await execution(session, node, accepted_plan_version=1, status="blocked", block_code="human_input_required")
            if node_index in (998, 999):
                await session.commit()
                response = await client.get("/orchestration/flows?limit=1")
                assert response.status_code == 200, response.text
                body = response.json()
                primary = next(flow for flow in body["flows"] if flow["id"] == compiled.flow_id)
                if node_index == 998:  # Exactly 1,000 candidates, including the preserved story.
                    assert primary["display_counts"]["stalled"] == 1
                    assert body["status_counts"]["attention_needed"] == 1
                else:  # The 1,001st candidate refuses the tenant-wide scope only.
                    assert primary["display_counts"]["stalled"] == 0
                    assert body["status_counts"]["attention_needed"] == 0
                    assert any(wave["display_counts"]["stalled"] == 1 for wave in primary["waves"])
                    detail = await client.get(f"/orchestration/flows/{compiled.flow_id}")
                    assert detail.status_code == 200, detail.text
                    assert next(item for item in detail.json()["nodes"] if item["id"] == story.id)["display_state"] == "stalled"


async def test_plan_receipt_overflow_refuses_tenant_but_keeps_complete_page(session, app_with_router):
    compiled = await persist_legacy_plan(session, fixtures.base_proposal(), fixtures.approval())
    story = await session.scalar(
        select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "story-a")
    )
    story.state, story.attempts = "running", 1
    await session.commit()
    await execution(session, story, accepted_plan_version=1, status="blocked", block_code="human_input_required")
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    await session.commit()

    overflow_flow = await seed_flow(session, slug="plan-overflow")
    overflow_node = await seed_node(session, overflow_flow, node_ref="unverified", state="running")
    overflow_node.attempts = 1
    await execution(session, overflow_node, accepted_plan_version=1, status="blocked", block_code="human_input_required")
    await session.execute(
        insert(OrchestrationAcceptedPlan),
        [
            {
                "org_id": fixtures.ORG_A,
                "flow_id": overflow_flow.id,
                "version": version,
                "plan_document": {},
                "plan_hash": "invalid",
                "superseded_at": datetime.now(UTC) if version < 10000 else None,
            }
            for version in range(1, 10001)
        ],
    )
    await session.commit()

    async with client_for(app_with_router) as client:
        response = await client.get("/orchestration/flows?limit=1")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 2
        primary = body["flows"][0]
        assert primary["id"] == compiled.flow_id
        assert primary["display_counts"]["stalled"] == 0
        assert body["status_counts"]["attention_needed"] == 0
        assert any(wave["display_counts"]["stalled"] == 1 for wave in primary["waves"])
        detail = await client.get(f"/orchestration/flows/{compiled.flow_id}")
        assert detail.status_code == 200, detail.text
        assert next(item for item in detail.json()["nodes"] if item["id"] == story.id)["display_state"] == "stalled"


async def test_postgres_snapshot_sets_read_only_isolation_before_projection(monkeypatch):
    from src.orchestration import routes

    events = []
    read_db = SimpleNamespace(info={})

    @asynccontextmanager
    async def transaction():
        events.append("begin")
        yield
        events.append("end transaction")

    @asynccontextmanager
    async def connection():
        events.append("connect")
        yield SimpleNamespace(
            begin=transaction,
            dialect=SimpleNamespace(name="postgresql"),
            execute=AsyncMock(side_effect=lambda sql: events.append(str(sql))),
        )
        events.append("close connection")

    @asynccontextmanager
    async def read_session(**kwargs):
        assert kwargs["expire_on_commit"] is False
        events.append("open read session")
        yield read_db
        events.append("close read session")

    monkeypatch.setattr(routes, "AsyncSession", read_session)
    db = SimpleNamespace(info={}, bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=connection))
    async with routes._flow_read_snapshot(db) as snapshot:
        assert snapshot is read_db
        events.append("project")
    events.append("provider")
    assert events == [
        "connect",
        "begin",
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        "open read session",
        "project",
        "close read session",
        "end transaction",
        "close connection",
        "provider",
    ]


async def test_graph_ends_relational_snapshot_before_optional_activity(monkeypatch):
    from src.orchestration import routes
    from src.orchestration.cost import AggregateCost, CostStatus

    active = False
    response = SimpleNamespace(nodes=[])

    @asynccontextmanager
    async def snapshot(_db):
        nonlocal active
        active = True
        yield object()
        active = False

    async def provider(**_kwargs):
        assert not active
        return {}

    async def insights(*_args, **_kwargs):
        assert active
        return {}

    monkeypatch.setattr(routes, "_flow_read_snapshot", snapshot)
    monkeypatch.setattr(routes, "_flow_graph_core", AsyncMock(return_value=(response, object(), [])))
    monkeypatch.setattr(routes, "get_display_flow_cost", AsyncMock(return_value=AggregateCost(address="flow", status=CostStatus.UNKNOWN)))
    monkeypatch.setattr(routes, "current_story_run_insights", insights)
    monkeypatch.setattr(routes, "load_story_execution", provider)
    access = SimpleNamespace(check_permission=AsyncMock())
    result = await routes.get_flow_graph("flow", SimpleNamespace(org_id="org-alpha"), access, object())
    assert result is response
    access.check_permission.assert_awaited_once()


async def test_second_request_sees_committed_amendment_in_list_and_graph(session, app_with_router):
    compiled = await persist_legacy_plan(session, fixtures.base_proposal(), fixtures.approval())
    await session.commit()
    async with client_for(app_with_router) as client:
        before = await client.get("/orchestration/flows?q=demo-flow&limit=1")
        before_graph = await client.get(f"/orchestration/flows/{compiled.flow_id}")
        assert before.status_code == before_graph.status_code == 200
        initial_nodes = before.json()["flows"][0]["total_nodes"]
        assert len(before_graph.json()["nodes"]) == initial_nodes
        await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
        await session.commit()
        after = await client.get("/orchestration/flows?q=demo-flow&limit=1")
        graph = await client.get(f"/orchestration/flows/{compiled.flow_id}")
        assert after.status_code == graph.status_code == 200
        assert after.json()["flows"][0]["total_nodes"] == initial_nodes + 1
        assert len(graph.json()["nodes"]) == initial_nodes + 1
        assert after.json()["total"] == 1
        assert sum(after.json()["status_counts"].values()) == 1


async def test_read_snapshot_preserves_empty_filtered_archived_and_paginated_lists(session, app_with_router):
    async with client_for(app_with_router) as client:
        empty = await client.get("/orchestration/flows")
        assert empty.status_code == 200 and empty.json()["total"] == 0
        assert empty.json()["flows"] == []

        compiled = await persist_legacy_plan(session, fixtures.base_proposal(), fixtures.approval())
        await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
        archived = await seed_flow(session, slug="archived-owned-flow")
        archived.archived_at = datetime.now(UTC)
        await session.commit()

        active = await client.get("/orchestration/flows?q=demo-flow&limit=1")
        assert active.status_code == 200 and active.json()["total"] == 1
        assert active.json()["flows"][0]["id"] == compiled.flow_id
        all_flows = await client.get("/orchestration/flows?include_archived=true&limit=1")
        second_page = await client.get("/orchestration/flows?include_archived=true&limit=1&offset=1")
        assert all_flows.status_code == second_page.status_code == 200
        assert all_flows.json()["total"] == second_page.json()["total"] == 2
        assert {all_flows.json()["flows"][0]["id"], second_page.json()["flows"][0]["id"]} == {compiled.flow_id, archived.id}
        assert sum(second_page.json()["status_counts"].values()) == 2
        missing = await client.get("/orchestration/flows?include_archived=true&q=absent")
        assert missing.status_code == 200 and missing.json()["total"] == 0
        assert missing.json()["flows"] == []
        assert sum(missing.json()["status_counts"].values()) == 2
        graph = await client.get(f"/orchestration/flows/{compiled.flow_id}")
        assert graph.status_code == 200 and len(graph.json()["nodes"]) == active.json()["flows"][0]["total_nodes"]


async def test_concurrent_amendment_cannot_mix_rows_and_chips(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'flow-read.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_wal(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA journal_mode=WAL")

    try:
        await _assert_concurrent_amendment(engine, monkeypatch, separate_sqlite=True)
    finally:
        await engine.dispose()


async def _assert_concurrent_amendment(engine, monkeypatch, *, separate_sqlite=False):
    from fastapi import FastAPI

    from src.orchestration import routes
    from src.shared.database import get_db

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        compiled = await persist_legacy_plan(db, fixtures.base_proposal(), fixtures.approval())
        await db.commit()

    if separate_sqlite:
        original_snapshot = routes._flow_read_snapshot
        monkeypatch.setattr(routes, "_flow_read_snapshot", lambda db: original_snapshot(db, separate_sqlite=True))
    original_page = OrchestrationRepository.list_flows_page_with_aggregates
    amended = False

    async def read_then_amend(self, **kwargs):
        nonlocal amended
        page = await original_page(self, **kwargs)
        if not amended:
            async with factory() as writer:
                await amend_plan(writer, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
                flow = await OrchestrationRepository(writer).get_flow(org_id=fixtures.ORG_A, flow_id=compiled.flow_id)
                flow.archived_at = datetime.now(UTC)
                await writer.commit()
            amended = True
        return page

    monkeypatch.setattr(OrchestrationRepository, "list_flows_page_with_aggregates", read_then_amend)
    app = FastAPI()
    app.include_router(routes.router)

    async def override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        async with client_for(app) as client:
            first = await client.get("/orchestration/flows")
            assert first.status_code == 200, first.text
            assert amended
            assert first.json()["total"] == 1
            assert first.json()["flows"][0]["total_nodes"] == 3
            assert first.json()["status_counts"]["archived"] == 0
            second = await client.get("/orchestration/flows?include_archived=true")
            assert second.status_code == 200, second.text
            assert second.json()["total"] == 1
            assert second.json()["flows"][0]["total_nodes"] == 4
            assert second.json()["status_counts"]["archived"] == 1
            graph = await client.get(f"/orchestration/flows/{compiled.flow_id}")
            assert graph.status_code == 200, graph.text
            assert len(graph.json()["nodes"]) == 4
    finally:
        monkeypatch.undo()


@pytest.fixture
async def postgres_session(pg_url):
    # Migration 107 validates the public-schema index it owns. Isolate at the
    # database boundary, as the other projection fixtures do, so this executes
    # the actual migration without weakening its ownership checks. pg_url also
    # supports external test servers through BG_TEST_POSTGRES_URI.
    engine = create_async_engine(to_async_url(pg_url))
    try:
        async with engine.connect() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.commit()
            await connection.run_sync(apply_migration)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()


@pytest.fixture
def postgres_app(postgres_session):
    from fastapi import FastAPI

    from src.orchestration import routes
    from src.shared.database import get_db

    app = FastAPI()
    app.include_router(routes.router)

    async def override_db():
        yield postgres_session

    app.dependency_overrides[get_db] = override_db
    return app


async def test_postgres_bounded_lineage_queries(postgres_session, postgres_app):
    await test_bounded_lineage_queries_cover_rows_chips_waves_and_graph(postgres_session, postgres_app)


@pytest.mark.parametrize("corruption", ["missing_receipt", "foreign_receipt", "missing_predecessor"])
async def test_postgres_lineage_refusals(postgres_session, postgres_app, corruption):
    await test_lineage_refusals_do_not_grant_stalled_state(postgres_session, postgres_app, corruption)


async def test_postgres_hop_boundaries(postgres_session, postgres_app):
    await test_lineage_refuses_101_hops_but_not_99_or_100(postgres_session, postgres_app)


async def test_postgres_candidate_overflow(postgres_session, postgres_app):
    await test_candidate_overflow_does_not_poison_smaller_page_waves(postgres_session, postgres_app)


async def test_postgres_plan_receipt_overflow(postgres_session, postgres_app):
    await test_plan_receipt_overflow_refuses_tenant_but_keeps_complete_page(postgres_session, postgres_app)


@pytest.mark.parametrize("hops", [1, 10, 30])
async def test_postgres_bounded_multi_hop_lineage(postgres_session, postgres_app, hops):
    await test_bounded_multi_hop_lineage_queries(postgres_session, postgres_app, hops)


async def test_postgres_read_after_amendment(postgres_session, postgres_app):
    await test_second_request_sees_committed_amendment_in_list_and_graph(postgres_session, postgres_app)


async def test_postgres_list_filters(postgres_session, postgres_app):
    await test_read_snapshot_preserves_empty_filtered_archived_and_paginated_lists(postgres_session, postgres_app)


async def test_postgres_concurrent_amendment_on_configured_fixture(postgres_session, monkeypatch):
    await _assert_concurrent_amendment(postgres_session.bind, monkeypatch)


@pytest.mark.parametrize("hops", [1, 10, 30])
async def test_bounded_multi_hop_lineage_queries(session, app_with_router, hops):
    first_flow_id = None
    first_story_id = None
    for flow_index in range(20):
        slug = f"scenario-{flow_index:02d}"
        proposal = fixtures.base_proposal(flow=slug)
        compiled = await persist_legacy_plan(session, proposal, fixtures.approval())
        story = await session.scalar(
            select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "story-a")
        )
        story.state, story.attempts = "running", 1
        await session.commit()
        await execution(session, story, accepted_plan_version=1, status="blocked", block_code="human_input_required")
        await amend_plan(
            session,
            flow_id=compiled.flow_id,
            proposal=fixtures.amended_proposal(flow=slug, extra_gate=False),
            actor=fixtures.amender(),
        )
        await session.commit()
        if flow_index == 0:
            first_flow_id, first_story_id = compiled.flow_id, story.id
            current = await session.scalar(
                select(OrchestrationAcceptedPlan).where(
                    OrchestrationAcceptedPlan.flow_id == compiled.flow_id, OrchestrationAcceptedPlan.superseded_at.is_(None)
                )
            )
            for version in range(3, hops + 2):
                current.superseded_at = datetime.now(UTC)
                decision = OrchestrationDecision(
                    org_id=fixtures.ORG_A,
                    flow_id=compiled.flow_id,
                    kind="plan_amended",
                    actor_id="owner",
                    actor_role="org_admin",
                    actor_kind="human",
                    reason="unchanged node definition",
                )
                session.add(decision)
                await session.flush()
                successor = OrchestrationAcceptedPlan(
                    org_id=fixtures.ORG_A,
                    flow_id=compiled.flow_id,
                    version=version,
                    plan_document=current.plan_document,
                    plan_hash=current.plan_hash,
                    accepted_by_decision_id=decision.id,
                )
                session.add(successor)
                await session.commit()
                current = successor
        flow = await OrchestrationRepository(session).get_flow(org_id=fixtures.ORG_A, flow_id=compiled.flow_id)
        for node_index in range(96):
            await seed_node(session, flow, node_ref=f"extra-{node_index:03d}")
        await session.commit()

    statements, stop = request_statement_recorder(session)
    try:
        async with client_for(app_with_router) as client:
            listing = await client.get("/orchestration/flows?limit=20")
            list_sql = list(statements)
            graph = await client.get(f"/orchestration/flows/{first_flow_id}")
    finally:
        stop()
    assert listing.status_code == graph.status_code == 200
    assert listing.json()["total"] == 20
    assert len(listing.json()["flows"]) == 20
    assert all(flow["total_nodes"] == 100 for flow in listing.json()["flows"])
    assert listing.json()["status_counts"]["attention_needed"] == 20
    assert next(node for node in graph.json()["nodes"] if node["id"] == first_story_id)["display_state"] == "stalled"
    list_lineage = lineage_statements(list_sql)
    graph_lineage = lineage_statements(statements[len(list_sql) :])
    other_sql = len(statements) - len(list_lineage) - len(graph_lineage)
    assert len(list_lineage) == 3, (hops, list_lineage)
    assert len(graph_lineage) == 3, (hops, graph_lineage)
    assert other_sql > 0


activity_session = activity_fixtures.session


@pytest.fixture
def activity_app_with_router(activity_session):
    return activity_fixtures.app_with_router.__wrapped__(activity_session)


@pytest.mark.asyncio
async def test_core_graph_skips_activity(activity_session, activity_app_with_router, monkeypatch):
    from src.activity.schemas import InvocationChainResponse

    flow = await activity_fixtures.seed_flow(activity_session)
    node = await activity_fixtures.seed_node(activity_session, flow, node_ref="story-a", state="running", attempts=1)
    await OrchestrationRepository(activity_session).append_decision(
        org_id=activity_fixtures.ORG_A,
        flow_id=flow.id,
        node_id=node.id,
        kind=DecisionKind.NODE_DISPATCHED.value,
        actor_id="service:dispatch",
        actor_role="service",
        actor_kind=ActorKind.SERVICE.value,
        reason=json.dumps({"attempt": 1, "run_id": "attempt-1", "repo": "aws-e/adp", "issue": 5037}),
    )
    service = MagicMock()
    service.get_chain.return_value = InvocationChainResponse(correlation_id="attempt-1", total_count=0, items=[])
    monkeypatch.setattr("src.orchestration.node_activity._activity_service", lambda: service)
    client = activity_fixtures.client_for(activity_app_with_router)

    core = client.get(f"{activity_fixtures.route(flow.id)}?include_activity=false")
    assert core.status_code == 200, core.text
    body = core.json()
    assert body["activity_included"] is False
    assert body["nodes"][0]["attempts"] == 1
    assert body["nodes"][0]["run_id"] == "attempt-1"
    assert body["nodes"][0]["activity"] is None
    assert body["nodes"][0]["execution_history"] is None
    service.get_chain.assert_not_called()

    legacy = client.get(activity_fixtures.route(flow.id))
    assert legacy.status_code == 200, legacy.text
    enriched = legacy.json()
    assert enriched["activity_included"] is True
    assert enriched["nodes"][0]["execution_history"]["run_id"] == "attempt-1"
    service.get_chain.assert_called_once_with(correlation_id="attempt-1", tenant_id=activity_fixtures.ORG_A)
    enriched.pop("activity_included")
    body.pop("activity_included")
    enriched["nodes"][0]["execution_history"] = None
    assert enriched == body


@pytest.mark.asyncio
async def test_activity_deadline_and_concurrency(monkeypatch):
    import asyncio
    from threading import Event, Lock
    from types import SimpleNamespace

    from src.activity.schemas import InvocationChainResponse
    from src.orchestration import node_activity

    started = Event()
    release = Event()
    lock = Lock()
    active = 0
    peak = 0
    calls = []

    monkeypatch.setattr(node_activity, "time", SimpleNamespace(monotonic=lambda: 100.0))

    class Reader:
        def get_chain_bounded(self, run_id, *, tenant_id, deadline, cancelled):
            nonlocal active, peak
            assert tenant_id == activity_fixtures.ORG_A
            with lock:
                calls.append(run_id)
                active += 1
                peak = max(peak, active)
                if active == 4:
                    started.set()
            try:
                assert release.wait(10)
                return InvocationChainResponse(correlation_id=run_id, total_count=0, items=[])
            finally:
                with lock:
                    active -= 1

    monkeypatch.setattr(node_activity, "_bounded_activity_service", Reader)
    run_ids = [f"run-{index}" for index in range(20)]
    task = asyncio.create_task(node_activity.load_bounded_story_execution(org_id=activity_fixtures.ORG_A, run_ids=run_ids, deadline=120.0))
    try:
        assert await asyncio.to_thread(started.wait, 5)
    finally:
        release.set()
        results = await asyncio.wait_for(task, 15)
    assert peak == 4
    assert active == 0
    assert sorted(calls) == sorted(run_ids)
    assert list(results) == run_ids
    assert all(execution is not None and execution.run_id == run_id and not execution.history_complete for run_id, execution in results.items())


@pytest.mark.parametrize("remaining_seconds,expected_calls", [(2.001, 1), (2.0, 0), (1.0, 0), (0.0, 0)])
def test_activity_read_respects_deadline_before_start(monkeypatch, remaining_seconds, expected_calls):
    from threading import Event
    from types import SimpleNamespace

    from src.activity.schemas import InvocationChainResponse
    from src.orchestration import node_activity

    service = MagicMock()
    service.get_chain_bounded.return_value = InvocationChainResponse(correlation_id="bounded-run", total_count=0, items=[])
    factory = MagicMock(return_value=service)
    monkeypatch.setattr(node_activity, "_bounded_activity_service", factory)
    monkeypatch.setattr(node_activity, "time", SimpleNamespace(monotonic=lambda: 100.0))
    cancelled = Event()
    deadline = 100.0 + remaining_seconds

    result = node_activity._read_bounded_execution(activity_fixtures.ORG_A, "bounded-run", deadline, cancelled)

    assert factory.call_count == service.get_chain_bounded.call_count == expected_calls
    if expected_calls:
        assert result.run_id == "bounded-run"
        service.get_chain_bounded.assert_called_once_with(
            "bounded-run", tenant_id=activity_fixtures.ORG_A, deadline=deadline, cancelled=cancelled.is_set
        )
    else:
        assert result is None


def test_activity_read_discards_result_at_deadline(monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from src.activity.schemas import InvocationChainResponse
    from src.orchestration import node_activity

    now = 100.0

    def read(*args, **kwargs):
        nonlocal now
        now = 110.0
        return InvocationChainResponse(correlation_id="bounded-run", total_count=0, items=[])

    service = MagicMock()
    service.get_chain_bounded.side_effect = read
    monkeypatch.setattr(node_activity, "_bounded_activity_service", lambda: service)
    monkeypatch.setattr(node_activity, "time", SimpleNamespace(monotonic=lambda: now))

    assert node_activity._read_bounded_execution(activity_fixtures.ORG_A, "bounded-run", 110.0, Event()) is None
    service.get_chain_bounded.assert_called_once()


@pytest.mark.asyncio
async def test_activity_errors_and_wrong_identity_remain_unavailable(monkeypatch):
    from botocore.exceptions import ClientError

    from src.activity.schemas import InvocationChainResponse
    from src.orchestration import node_activity

    # This checks provider failures and identity, independently of runner load.
    # Deadline expiry has separate boundary and cancellation coverage.
    monkeypatch.setattr(node_activity, "time", SimpleNamespace(monotonic=lambda: 100.0))

    class Reader:
        def get_chain_bounded(self, run_id, *, tenant_id, deadline, cancelled):
            if run_id == "error":
                raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}}, "Query")
            return InvocationChainResponse(correlation_id="foreign" if run_id == "wrong" else run_id, total_count=0, items=[])

    monkeypatch.setattr(node_activity, "_bounded_activity_service", Reader)
    result = await node_activity.load_bounded_story_execution(org_id=activity_fixtures.ORG_A, run_ids=["error", "wrong", "valid"], deadline=120.0)
    assert result["error"] is None
    assert result["wrong"] is None
    assert result["valid"].run_id == "valid"
    assert result["valid"].history_complete is False


@pytest.mark.asyncio
async def test_cancelled_activity_request_drains_without_scheduling_new_reads(monkeypatch):
    import asyncio
    import time
    from threading import Event, Lock

    from src.activity.schemas import InvocationChainResponse
    from src.orchestration import node_activity

    started = Event()
    release = Event()
    drained = Event()
    lock = Lock()
    active = 0
    calls = 0

    class Reader:
        def get_chain_bounded(self, run_id, *, tenant_id, deadline, cancelled):
            nonlocal active, calls
            with lock:
                calls += 1
                active += 1
                started.set()
            try:
                assert release.wait(2)
                return InvocationChainResponse(correlation_id=run_id, total_count=0, items=[])
            finally:
                with lock:
                    active -= 1
                    if active == 0:
                        drained.set()

    monkeypatch.setattr(node_activity, "_bounded_activity_service", Reader)
    task = asyncio.create_task(
        node_activity.load_bounded_story_execution(
            org_id=activity_fixtures.ORG_A, run_ids=[f"run-{index}" for index in range(20)], deadline=time.monotonic() + 3
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        calls_after_cancel = calls
    finally:
        release.set()
    assert await asyncio.to_thread(drained.wait, 1)
    assert calls == calls_after_cancel <= 4


@pytest.mark.asyncio
async def test_expired_activity_batch_starts_no_provider_reads(monkeypatch):
    import time

    from src.orchestration import node_activity

    service = MagicMock()
    monkeypatch.setattr(node_activity, "_bounded_activity_service", service)
    result = await node_activity.load_bounded_story_execution(org_id=activity_fixtures.ORG_A, run_ids=["run-1"], deadline=time.monotonic() - 1)
    assert result == {"run-1": None}
    service.assert_not_called()


def test_bounded_activity_reuses_private_one_attempt_clients(monkeypatch):
    from threading import Thread, local

    from src.orchestration import node_activity

    monkeypatch.setattr(node_activity, "_bounded_services", local())
    resource = object()
    activity_session = MagicMock()
    activity_session.resource.return_value = resource
    monkeypatch.setattr(node_activity.boto3.session, "Session", lambda: activity_session)
    monkeypatch.setattr(node_activity, "ActivityService", lambda dynamodb_resource: dynamodb_resource)
    assert node_activity._bounded_activity_service() is resource
    assert node_activity._bounded_activity_service() is resource
    assert activity_session.resource.call_count == 1
    worker = Thread(target=node_activity._bounded_activity_service)
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert activity_session.resource.call_count == 2
    config = activity_session.resource.call_args.kwargs["config"]
    assert config.connect_timeout == 1
    assert config.read_timeout == 1
    assert config.retries["total_max_attempts"] == 1


@pytest.mark.asyncio
async def test_activity_batch_deadline_discards_late_completion(monkeypatch):
    import asyncio
    import time
    from threading import Event

    from src.orchestration import node_activity

    started = Event()
    release = Event()
    finished = Event()

    def slow_read(org_id, run_id, deadline, cancelled, continuation=None):
        started.set()
        release.wait(1)
        finished.set()
        return node_activity.StoryExecution(run_id=run_id)

    monkeypatch.setattr(node_activity, "_read_bounded_execution", slow_read)
    task = asyncio.create_task(
        node_activity.load_bounded_story_execution(org_id=activity_fixtures.ORG_A, run_ids=["run-1"], deadline=time.monotonic() + 0.1)
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        result = await asyncio.wait_for(task, 1)
        assert result == {"run-1": None}
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 1)


@pytest.mark.asyncio
async def test_story_selection_requires_complete_tenant_set(activity_session):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from src.orchestration.routes import _authorize_story_selection

    flow = await activity_fixtures.seed_flow(activity_session)
    first = await activity_fixtures.seed_node(activity_session, flow, node_ref="story-a")
    second = await activity_fixtures.seed_node(activity_session, flow, node_ref="story-b")
    gate = await activity_fixtures.seed_node(activity_session, flow, node_ref="gate-a", kind="gate")
    foreign_flow = await activity_fixtures.seed_flow(activity_session, org_id=activity_fixtures.ORG_B)
    foreign = await activity_fixtures.seed_node(activity_session, foreign_flow, node_ref="foreign-a", org_id=activity_fixtures.ORG_B)
    principal = SimpleNamespace(org_id=activity_fixtures.ORG_A)

    async def select_nodes(node_ids, flow_id=flow.id):
        return await _authorize_story_selection(db=activity_session, current_user=principal, flow_id=flow_id, node_ids=node_ids)

    assert [row.id for row in await select_nodes([second.id, first.id])] == [second.id, first.id]

    for selection in (None, [], [""], [" "], [first.id, first.id], [first.id] * 21, [gate.id]):
        with pytest.raises(HTTPException) as error:
            await select_nodes(selection)
        assert error.value.status_code == 422
    for selection, selected_flow in (([first.id, "absent"], flow.id), ([first.id, foreign.id], flow.id), ([foreign.id], foreign_flow.id)):
        with pytest.raises(HTTPException) as error:
            await select_nodes(selection, selected_flow)
        assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_current_binding_uses_latest_qualifying_dispatch_and_attempt(activity_session):
    import time
    from datetime import UTC, datetime

    from sqlalchemy import event, insert

    from src.orchestration.models import OrchestrationDecision
    from src.orchestration.routes import _current_story_bindings

    flow = await activity_fixtures.seed_flow(activity_session)
    current = await activity_fixtures.seed_node(activity_session, flow, node_ref="current", state="running", attempts=2)
    obsolete = await activity_fixtures.seed_node(activity_session, flow, node_ref="obsolete", state="running", attempts=2)
    pending = await activity_fixtures.seed_node(activity_session, flow, node_ref="pending", state="pending", attempts=2)
    foreign_flow = await activity_fixtures.seed_flow(activity_session, org_id=activity_fixtures.ORG_B)
    foreign = await activity_fixtures.seed_node(activity_session, foreign_flow, node_ref="foreign", org_id=activity_fixtures.ORG_B)
    timestamp = datetime.now(UTC)
    entries = [
        ("01", current, {"attempt": 1, "run_id": "old"}, activity_fixtures.ORG_A, flow.id),
        ("02", current, {"attempt": 2, "run_id": "right"}, activity_fixtures.ORG_A, flow.id),
        ("03", current, "{", activity_fixtures.ORG_A, flow.id),
        ("04", obsolete, {"attempt": 2, "run_id": "stale"}, activity_fixtures.ORG_A, flow.id),
        ("05", obsolete, {"attempt": 1, "run_id": "newer-attempt"}, activity_fixtures.ORG_A, flow.id),
        ("06", pending, {"attempt": 2, "run_id": "not-eligible"}, activity_fixtures.ORG_A, flow.id),
        ("07", foreign, {"attempt": 2, "run_id": "foreign"}, activity_fixtures.ORG_B, foreign_flow.id),
    ]
    rows = [
        {
            "id": f"{int(identifier):036d}",
            "org_id": org_id,
            "flow_id": entry_flow,
            "node_id": node.id,
            "kind": DecisionKind.NODE_DISPATCHED.value,
            "actor_id": "service:dispatch",
            "actor_role": "service",
            "actor_kind": ActorKind.SERVICE.value,
            "reason": json.dumps(detail) if isinstance(detail, dict) else detail,
            "created_at": timestamp,
        }
        for identifier, node, detail, org_id, entry_flow in entries
    ]
    await activity_session.execute(insert(OrchestrationDecision.__table__), rows)
    statements = []

    def count_decision_reads(_connection, _cursor, statement, _params, _context, _executemany):
        if "orchestration_decisions" in statement and statement.lstrip().startswith(("SELECT", "WITH")):
            statements.append(statement)

    event.listen(activity_session.bind.sync_engine, "before_cursor_execute", count_decision_reads)
    try:
        bindings = await _current_story_bindings(
            db=activity_session,
            org_id=activity_fixtures.ORG_A,
            flow_id=flow.id,
            nodes=[current, obsolete, pending],
            deadline=time.monotonic() + 3,
        )
    finally:
        event.remove(activity_session.bind.sync_engine, "before_cursor_execute", count_decision_reads)
    assert bindings == {current.id: "right", obsolete.id: None, pending.id: None}
    assert len(statements) == 1
    assert "LIMIT" in statements[0]


@pytest.mark.asyncio
async def test_current_binding_long_history_and_expired_deadline(activity_session):
    import time
    from datetime import UTC, datetime

    from fastapi import HTTPException
    from sqlalchemy import insert

    from src.orchestration.models import OrchestrationDecision
    from src.orchestration.routes import _current_story_bindings

    flow = await activity_fixtures.seed_flow(activity_session)
    node = await activity_fixtures.seed_node(activity_session, flow, node_ref="story-a", state="running", attempts=1)
    records = [
        {
            "id": f"{number:036d}",
            "org_id": activity_fixtures.ORG_A,
            "flow_id": flow.id,
            "node_id": node.id,
            "kind": DecisionKind.NODE_DISPATCHED.value,
            "actor_id": "service:dispatch",
            "actor_role": "service",
            "actor_kind": ActorKind.SERVICE.value,
            "reason": json.dumps({"attempt": 1, "run_id": "old"}),
            "created_at": datetime.now(UTC),
        }
        for number in range(1001)
    ]
    await activity_session.execute(insert(OrchestrationDecision.__table__), records)
    bindings = await _current_story_bindings(
        db=activity_session, org_id=activity_fixtures.ORG_A, flow_id=flow.id, nodes=[node], deadline=time.monotonic() + 3
    )
    assert bindings == {node.id: "old"}
    with pytest.raises(HTTPException) as error:
        await _current_story_bindings(
            db=activity_session, org_id=activity_fixtures.ORG_A, flow_id=flow.id, nodes=[node], deadline=time.monotonic() - 1
        )
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_activity_scope_and_attempt(activity_session, activity_app_with_router, monkeypatch):
    from datetime import datetime
    from unittest.mock import AsyncMock

    from src.orchestration.node_activity import StoryExecution

    flow = await activity_fixtures.seed_flow(activity_session)
    available = await activity_fixtures.seed_node(activity_session, flow, node_ref="available", state="running", attempts=1)
    unavailable = await activity_fixtures.seed_node(activity_session, flow, node_ref="unavailable", state="running", attempts=1)
    changed = await activity_fixtures.seed_node(activity_session, flow, node_ref="changed", state="running", attempts=2)
    pending = await activity_fixtures.seed_node(activity_session, flow, node_ref="pending", state="pending", attempts=1)
    for node, run_id in ((available, "server-a"), (unavailable, "server-b"), (changed, "old"), (pending, "queued")):
        await OrchestrationRepository(activity_session).append_decision(
            org_id=activity_fixtures.ORG_A,
            flow_id=flow.id,
            node_id=node.id,
            kind=DecisionKind.NODE_DISPATCHED.value,
            actor_id="service:dispatch",
            actor_role="service",
            actor_kind=ActorKind.SERVICE.value,
            reason=json.dumps({"attempt": 1, "run_id": run_id}),
        )

    async def provider(*, org_id, run_ids, deadline, continuations):
        assert not activity_session.in_transaction()
        assert continuations == {}
        assert org_id == activity_fixtures.ORG_A
        assert run_ids == ["server-b", "server-a"]
        return {"server-b": None, "server-a": StoryExecution(run_id="server-a")}

    loader = AsyncMock(side_effect=provider)
    monkeypatch.setattr("src.orchestration.routes.load_bounded_story_execution", loader)
    requested = [unavailable, changed, available, pending]
    client = activity_fixtures.client_for(activity_app_with_router)
    response = client.get(
        f"{activity_fixtures.route(flow.id)}/activity",
        params=[*(("node_ids", node.id) for node in requested), ("run_id", "forged")],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"flow_id", "observed_at", "items"}
    assert body["flow_id"] == flow.id
    assert datetime.fromisoformat(body["observed_at"].replace("Z", "+00:00")).utcoffset().total_seconds() == 0
    assert body["items"] == [
        {
            "node_id": unavailable.id,
            "attempt": 1,
            "run_id": "server-b",
            "availability": "unavailable",
            "execution_history": None,
            "run_insight": None,
        },
        {"node_id": changed.id, "attempt": 2, "run_id": None, "availability": "not_started", "execution_history": None, "run_insight": None},
        {
            "node_id": available.id,
            "attempt": 1,
            "run_id": "server-a",
            "availability": "available",
            "execution_history": StoryExecution(run_id="server-a").model_dump(),
            "run_insight": None,
        },
        {"node_id": pending.id, "attempt": 1, "run_id": None, "availability": "not_started", "execution_history": None, "run_insight": None},
    ]


@pytest.mark.parametrize("continuation_cycle", [1, 2])
async def test_activity_does_not_attach_new_cycle_to_captured_attempt(activity_session, activity_app_with_router, monkeypatch, continuation_cycle):
    from src.orchestration.continuation_activity import ContinuationActivity
    from src.orchestration.node_activity import StoryExecution

    flow = await activity_fixtures.seed_flow(activity_session)
    node = await activity_fixtures.seed_node(activity_session, flow, node_ref="recovered", state="running", attempts=1)
    await OrchestrationRepository(activity_session).append_decision(
        org_id=activity_fixtures.ORG_A,
        flow_id=flow.id,
        node_id=node.id,
        kind=DecisionKind.NODE_DISPATCHED.value,
        actor_id="service:dispatch",
        actor_role="service",
        actor_kind=ActorKind.SERVICE.value,
        reason=json.dumps({"attempt": 1, "run_id": "original"}),
    )
    continuation = ContinuationActivity("reviewer", flow.id, "agent-codex-reviewer", continuation_cycle)
    monkeypatch.setattr("src.orchestration.routes.current_continuations", AsyncMock(return_value={node.id: continuation}))
    loader = AsyncMock(return_value={"original": StoryExecution(run_id="original")})
    monkeypatch.setattr("src.orchestration.routes.load_bounded_story_execution", loader)
    response = activity_fixtures.client_for(activity_app_with_router).get(
        f"{activity_fixtures.route(flow.id)}/activity", params={"node_ids": node.id}
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["attempt"] == 1
    assert loader.call_args.kwargs["continuations"] == ({"original": continuation} if continuation_cycle == 1 else {})
    loader.assert_awaited_once()


@pytest.mark.asyncio
async def test_activity_rejects_invalid_or_foreign_selection_before_provider(activity_session, activity_app_with_router, monkeypatch):
    from unittest.mock import AsyncMock

    flow = await activity_fixtures.seed_flow(activity_session)
    story = await activity_fixtures.seed_node(activity_session, flow, node_ref="story")
    gate = await activity_fixtures.seed_node(activity_session, flow, node_ref="gate", kind="gate")
    foreign_flow = await activity_fixtures.seed_flow(activity_session, org_id=activity_fixtures.ORG_B)
    foreign = await activity_fixtures.seed_node(activity_session, foreign_flow, node_ref="foreign", org_id=activity_fixtures.ORG_B)
    loader = AsyncMock()
    monkeypatch.setattr("src.orchestration.routes.load_bounded_story_execution", loader)
    client = activity_fixtures.client_for(activity_app_with_router)
    url = f"{activity_fixtures.route(flow.id)}/activity"

    for selection in (
        [],
        [("node_ids", "")],
        [("node_ids", story.id)] * 2,
        [("node_ids", f"{number:036d}") for number in range(21)],
        [("node_ids", gate.id)],
    ):
        response = client.get(url, params=selection)
        assert response.status_code == 422, response.text
    for selection in ([("node_ids", story.id), ("node_ids", "missing")], [("node_ids", story.id), ("node_ids", foreign.id)]):
        response = client.get(url, params=selection)
        assert response.status_code == 404, response.text
    assert (
        activity_fixtures.client_for(activity_app_with_router, org_id=activity_fixtures.ORG_B).get(url, params={"node_ids": story.id}).status_code
        == 404
    )
    assert activity_fixtures.client_for(activity_app_with_router, permitted=False).get(url, params={"node_ids": story.id}).status_code == 403
    loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_activity_long_history_uses_current_dispatch(activity_session, activity_app_with_router, monkeypatch):
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from sqlalchemy import insert

    from src.orchestration.models import OrchestrationDecision

    flow = await activity_fixtures.seed_flow(activity_session)
    node = await activity_fixtures.seed_node(activity_session, flow, node_ref="story", state="running", attempts=1)
    rows = [
        {
            "id": f"{number:036d}",
            "org_id": activity_fixtures.ORG_A,
            "flow_id": flow.id,
            "node_id": node.id,
            "kind": DecisionKind.NODE_DISPATCHED.value,
            "actor_id": "service:dispatch",
            "actor_role": "service",
            "actor_kind": ActorKind.SERVICE.value,
            "reason": json.dumps({"attempt": 1, "run_id": "run-1"}),
            "created_at": datetime.now(UTC),
        }
        for number in range(1001)
    ]
    await activity_session.execute(insert(OrchestrationDecision.__table__), rows)
    loader = AsyncMock(return_value={})
    monkeypatch.setattr("src.orchestration.routes.load_bounded_story_execution", loader)
    response = activity_fixtures.client_for(activity_app_with_router).get(
        f"{activity_fixtures.route(flow.id)}/activity", params={"node_ids": node.id}
    )
    assert response.status_code == 200, response.text
    loader.assert_awaited_once()
    assert loader.call_args.kwargs["run_ids"] == ["run-1"]


@pytest.mark.asyncio
async def test_activity_authorization_timeout_starts_no_provider_reads(activity_session, activity_app_with_router, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from src.orchestration import routes

    flow = await activity_fixtures.seed_flow(activity_session)
    story = await activity_fixtures.seed_node(activity_session, flow, node_ref="story")
    loader = AsyncMock()
    bindings = AsyncMock()
    monkeypatch.setattr(routes, "load_bounded_story_execution", loader)
    monkeypatch.setattr(routes, "_current_story_bindings", bindings)
    monkeypatch.setattr(routes, "time", SimpleNamespace(monotonic=lambda: 100.0))
    timeouts = []
    cancelled = False

    def capture_timeout(deadline):
        assert deadline == 105.0
        # Keep asyncio's real cancellation/TimeoutError conversion, but expire
        # only once authorization has started, independently of runner load.
        timeout = asyncio.timeout_at(None)
        timeouts.append(timeout)
        return timeout

    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(timeout_at=capture_timeout))

    async def slow_selection(**_kwargs):
        nonlocal cancelled
        timeouts[0].reschedule(asyncio.get_running_loop().time())
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    monkeypatch.setattr(routes, "_authorize_story_selection", slow_selection)
    response = activity_fixtures.client_for(activity_app_with_router).get(
        f"{activity_fixtures.route(flow.id)}/activity", params={"node_ids": story.id}
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "story activity binding unavailable"
    assert len(timeouts) == 1
    assert timeouts[0].expired()
    assert cancelled
    bindings.assert_not_awaited()
    loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_activity_http_deadline_for_twenty_mixed_stories(activity_session, activity_app_with_router, monkeypatch):
    import time
    from threading import Event

    from src.activity.schemas import InvocationChainResponse
    from src.orchestration import node_activity

    flow = await activity_fixtures.seed_flow(activity_session)
    nodes = []
    for index in range(20):
        node = await activity_fixtures.seed_node(activity_session, flow, node_ref=f"story-{index}", state="running", attempts=1)
        nodes.append(node)
        await OrchestrationRepository(activity_session).append_decision(
            org_id=activity_fixtures.ORG_A,
            flow_id=flow.id,
            node_id=node.id,
            kind=DecisionKind.NODE_DISPATCHED.value,
            actor_id="service:dispatch",
            actor_role="service",
            actor_kind=ActorKind.SERVICE.value,
            reason=json.dumps({"attempt": 1, "run_id": f"run-{index}"}),
        )

    release = Event()

    class Reader:
        def get_chain_bounded(self, run_id, *, tenant_id, deadline, cancelled):
            if int(run_id.split("-")[-1]) >= 4:
                release.wait(6)
            return InvocationChainResponse(correlation_id=run_id, total_count=0, items=[])

    monkeypatch.setattr(node_activity, "_bounded_activity_service", Reader)
    with activity_fixtures.client_for(activity_app_with_router) as client:
        started = time.monotonic()
        try:
            response = client.get(f"{activity_fixtures.route(flow.id)}/activity", params=[("node_ids", node.id) for node in nodes])
            elapsed = time.monotonic() - started
        finally:
            release.set()
    assert elapsed <= 5.5
    assert response.status_code == 200, response.text
    assert [item["node_id"] for item in response.json()["items"]] == [node.id for node in nodes]
    assert [item["availability"] for item in response.json()["items"]] == ["available"] * 4 + ["unavailable"] * 16
    assert all(item["execution_history"] is None for item in response.json()["items"][4:])


@pytest.mark.asyncio
async def test_incomplete_activity_binding_starts_no_provider_reads(activity_session, activity_app_with_router, monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    flow = await activity_fixtures.seed_flow(activity_session)
    story = await activity_fixtures.seed_node(activity_session, flow, node_ref="story")
    loader = AsyncMock()
    monkeypatch.setattr("src.orchestration.routes.load_bounded_story_execution", loader)
    monkeypatch.setattr("src.orchestration.routes._current_story_bindings", AsyncMock(side_effect=HTTPException(status_code=503)))
    response = activity_fixtures.client_for(activity_app_with_router).get(
        f"{activity_fixtures.route(flow.id)}/activity", params={"node_ids": story.id}
    )
    assert response.status_code == 503, response.text
    loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_activity_requires_authenticated_user_before_provider(activity_session, activity_app_with_router, monkeypatch):
    from unittest.mock import AsyncMock

    from src.auth.dependencies import get_current_user

    flow = await activity_fixtures.seed_flow(activity_session)
    node = await activity_fixtures.seed_node(activity_session, flow, node_ref="story")
    loader = AsyncMock()
    monkeypatch.setattr("src.orchestration.routes.load_bounded_story_execution", loader)
    client = activity_fixtures.client_for(activity_app_with_router)
    activity_app_with_router.dependency_overrides.pop(get_current_user)
    response = client.get(f"{activity_fixtures.route(flow.id)}/activity", params={"node_ids": node.id})
    assert response.status_code == 401, response.text
    loader.assert_not_awaited()


def assert_bounded_query_growth(counts):
    assert counts[30] <= counts[1] + 5, f"per-hop SQL growth: {counts}"


async def measure_read_queries(session, *, org_id, flow_id):
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        states = await OrchestrationRepository(session).node_display_states(org_id=org_id, flow_id=flow_id)
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
    return len(statements), states


@pytest.fixture
def required_pg_url(request):
    try:
        return request.getfixturevalue("pg_url")
    except pytest.skip.Exception as exc:
        pytest.fail(f"PostgreSQL 16 is required for the flow-read gate: {exc}")


@pytest.fixture
async def populated_flows(required_pg_url):
    engine = create_async_engine(to_async_url(required_pg_url), connect_args={"server_settings": {"statement_timeout": "15000"}})
    try:
        async with engine.begin() as connection:
            version = int((await connection.scalar(text("SHOW server_version_num")))[:2])
            assert version == 16, f"flow-read gate requires PostgreSQL 16, got {version}"
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        model.__table__
                        for model in (
                            OrchestrationFlow,
                            OrchestrationNode,
                            OrchestrationAcceptedPlan,
                            OrchestrationDecision,
                            OrchestrationExecution,
                            OrchestrationWorkClaim,
                            UsageLog,
                        )
                    ],
                )
            )
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            manifest = await populate_flows(session)
            await session.commit()
            yield session, manifest
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_history_scale_regression(populated_flows, record_property):
    session, manifest = populated_flows
    record_property(
        "fixture_manifest",
        json.dumps(
            {
                key: manifest[key]
                for key in (
                    "tenants",
                    "flow_count",
                    "nodes_per_flow",
                    "decision_count",
                    "history_count",
                    "amendment_hops",
                    "versions",
                    "clock",
                    "seed",
                    "expected_states",
                    "expected_costs",
                )
            },
            sort_keys=True,
        ),
    )
    record_property("fixture_source_sha256", hashlib.sha256((Path(__file__).parent / "flow_read_fixture.py").read_bytes()).hexdigest())
    record_property(
        "source_revision",
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[4],
            text=True,
        ).strip(),
    )
    assert manifest["amendment_hops"] == (1, 10, 30)
    assert manifest["versions"] == (2, 11, 31)
    assert len(manifest["flow_ids"]) == 20
    assert await session.scalar(select(func.count()).select_from(OrchestrationFlow)) == 20
    assert await session.scalar(select(func.count()).select_from(OrchestrationNode)) == 2_000
    assert await session.scalar(select(func.count()).select_from(OrchestrationNode).where(OrchestrationNode.state == "pending")) == 1_997
    assert await session.scalar(select(func.count()).select_from(OrchestrationDecision)) == 65_000
    assert await session.scalar(select(func.count()).select_from(OrchestrationDecision).where(OrchestrationDecision.kind == "node_dispatched")) == 34
    plan_count = select(func.count()).select_from(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == manifest["flow_ids"][2])
    assert await session.scalar(plan_count) == 31
    assert set((await session.scalars(select(OrchestrationFlow.org_id).distinct())).all()) == {"fixture-tenant-a", "fixture-tenant-b"}
    for flow_number, amount in ((0, "1.250000"), (1, "9.750000")):
        flow = await session.get(OrchestrationFlow, manifest["flow_ids"][flow_number])
        nodes = (await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == flow.id))).all()
        result = await get_flow_cost(session, org_id=flow.org_id, flow=flow, nodes=nodes)
        assert result.status == CostStatus.KNOWN
        assert str(result.amount_usd) == amount
        assert result.unknown_node_count == 99


@pytest.mark.integration
async def test_bounded_history_queries(populated_flows):
    session, manifest = populated_flows
    counts = {}
    for flow_number, hops in enumerate((1, 10, 30)):
        tenant = manifest["tenants"][flow_number % 2]
        flow_id = manifest["flow_ids"][flow_number]
        session.expire_all()
        counts[hops], states = await measure_read_queries(session, org_id=tenant, flow_id=flow_id)
        assert len(states) == 100
        assert list(states.values()).count("in_progress") == 1
        assert list(states.values()).count("queued") == 99
        foreign_tenant = manifest["tenants"][(flow_number + 1) % 2]
        _, foreign_states = await measure_read_queries(session, org_id=foreign_tenant, flow_id=flow_id)
        assert foreign_states == {}
    assert_bounded_query_growth(counts)


@pytest.mark.integration
async def test_injected_per_hop_query_regression_fails(populated_flows, monkeypatch):
    session, manifest = populated_flows
    flow_id = manifest["flow_ids"][2]
    tenant = manifest["tenants"][0]
    normal, states = await measure_read_queries(session, org_id=tenant, flow_id=flow_id)
    assert len(states) == 100
    original = plan_lineage.ancestor_plan

    async def extra_per_hop_sql(db, plan, version, *, node_id=None, _lookups=None):
        for _ in range(plan.version - version):
            await db.execute(text("SELECT 1"))
        return await original(db, plan, version, node_id=node_id, _lookups=_lookups)

    monkeypatch.setattr(plan_lineage, "ancestor_plan", extra_per_hop_sql)
    session.expire_all()
    injected, states = await measure_read_queries(session, org_id=tenant, flow_id=flow_id)
    assert len(states) == 100
    assert injected >= normal + 30
    with pytest.raises(AssertionError, match="per-hop SQL growth"):
        assert_bounded_query_growth({1: normal, 30: injected})
