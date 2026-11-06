"""Policy absence checks retain fail-closed semantics without copying old plans."""

# ruff: noqa: F811 -- pytest fixtures are imported for discovery
from datetime import UTC, datetime

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationFlow
from src.orchestration.policy_admission import display_policy_from_plan, load_in_force_policy
from src.shared.models.base import Base
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401


@pytest.fixture(params=["sqlite", "postgresql"])
async def db(request):
    url = "sqlite+aiosqlite:///:memory:"
    if request.param == "postgresql":
        url = to_async_url(request.getfixturevalue("pg_url"))
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[OrchestrationFlow.__table__, OrchestrationAcceptedPlan.__table__])
        )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("document", "withdrawn"),
    [
        ({}, False),
        ({"execution_policy": None}, False),
        (None, False),
        ({"execution_policy": False}, True),
        ({"execution_policy": 0}, True),
        ({"execution_policy": {}}, True),
        ({"execution_policy": []}, True),
        ({"execution_policy": ""}, True),
        ([], True),
        ("corrupt", True),
        (False, True),
    ],
)
async def test_historical_policy_absence_and_malformed_values(db, document, withdrawn):
    flow = OrchestrationFlow(org_id="org-a", slug="flow", title="Flow")
    db.add(flow)
    await db.flush()
    old = OrchestrationAcceptedPlan(
        org_id=flow.org_id, flow_id=flow.id, version=1, plan_document=document, plan_hash="old", superseded_at=datetime.now(UTC)
    )
    current = OrchestrationAcceptedPlan(org_id=flow.org_id, flow_id=flow.id, version=2, plan_document={}, plan_hash="current")
    db.add_all([old, current])
    await db.flush()
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", record)
    try:
        display = await display_policy_from_plan(db, org_id=flow.org_id, flow_id=flow.id, plan=current)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", record)
    assert (display.refusal is not None) is withdrawn
    assert display.plan_version == 2
    assert len(statements) == 1 and statements[0].startswith("SELECT EXISTS")
    assert await load_in_force_policy(db, org_id=flow.org_id, flow_id=flow.id) == display


async def test_history_exists_is_scoped_and_display_rejects_wrong_plan(db):
    flows = [OrchestrationFlow(org_id=org, slug=slug, title=slug) for org, slug in [("org-a", "one"), ("org-a", "two"), ("org-b", "one")]]
    db.add_all(flows)
    await db.flush()
    for flow in flows[1:]:
        db.add(OrchestrationAcceptedPlan(org_id=flow.org_id, flow_id=flow.id, plan_document={"execution_policy": {}}, plan_hash="history"))
    current = OrchestrationAcceptedPlan(org_id="org-a", flow_id=flows[0].id, plan_document={}, plan_hash="current")
    db.add(current)
    await db.flush()
    assert (await display_policy_from_plan(db, org_id="org-a", flow_id=flows[0].id, plan=current)).refusal is None
    for org, flow_id in [("org-b", current.flow_id), ("org-a", flows[1].id)]:
        with pytest.raises(ValueError, match="same flow and tenant"):
            await display_policy_from_plan(db, org_id=org, flow_id=flow_id, plan=current)
    current.superseded_at = datetime.now(UTC)
    with pytest.raises(ValueError):
        await display_policy_from_plan(db, org_id="org-a", flow_id=current.flow_id, plan=current)
