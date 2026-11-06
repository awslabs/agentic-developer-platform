"""Independent PostgreSQL reviewers cannot complete an operation twice."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src import knowledge_evidence as evidence
from src.agentauth.human_control import authorize_human_session
from src.knowledge_evidence_contract import Complete
from src.l5_integration import GatewayConfigurationBackend
from src.orchestration.l5_observation_scope import ObservationError
from src.products.knowledge_evidence_model import KnowledgeUseEvidence
from tests.products.test_knowledge_evidence import completed_publication as completed_publication
from tests.products.test_knowledge_evidence import prepare, record
from tests.products.test_l5_knowledge_effects_postgres import (
    claimed_knowledge as claimed_knowledge,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    consent_fixture as consent_fixture,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    knowledge_fixture as knowledge_fixture,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    knowledge_worker as knowledge_worker,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    l5_owner as l5_owner,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    pg_server as pg_server,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    pg_url as pg_url,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    test_engine as test_engine,
)

__all__ = [
    "completed_publication",
    "claimed_knowledge",
    "consent_fixture",
    "knowledge_fixture",
    "knowledge_worker",
    "l5_owner",
    "pg_server",
    "pg_url",
    "test_engine",
]


@pytest.fixture(autouse=True)
async def migration_required_before_metadata(test_engine):
    async with test_engine.connect() as connection:
        assert await connection.run_sync(lambda db: inspect(db).has_table(KnowledgeUseEvidence.__tablename__))


@pytest.mark.parametrize("same_key", [True, False])
async def test_independent_completion_serializes_and_retains_one_review(
    test_client, db_session, test_engine, completed_publication, l5_owner, monkeypatch, same_key
):
    fixture = completed_publication
    prepared = await prepare(test_client, fixture)
    assert prepared.status_code == 200, prepared.text
    observed = await record(test_client, fixture, prepared.json())
    assert observed.status_code == 200, observed.text
    review = Complete.model_validate(
        {
            "reviews": [
                {
                    "evidence_id": prepared.json()["evidence_id"],
                    "observation_hash": observed.json()["event_hash"],
                    "source_effect_explanation": "The real native result follows the exact selected source policy and its citation.",
                }
            ]
        }
    )
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    locked, competing = asyncio.Event(), asyncio.Event()
    original = evidence.scope
    entries, lock_queries = 0, 0

    async def hold(*args, **kwargs):
        nonlocal entries
        result = await original(*args, **kwargs)
        entries += 1
        if entries == 1:
            locked.set()
            await asyncio.wait_for(competing.wait(), 10)
        return result

    def query(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal lock_queries
        if "l5_service_configurations" in statement and "FOR UPDATE" in statement:
            lock_queries += 1
            if lock_queries >= 2:
                competing.set()

    async def complete(key):
        async with sessions() as db:
            human = await authorize_human_session(l5_owner, db)
            backend = GatewayConfigurationBackend(db, l5_owner)
            return await evidence.complete(
                backend,
                human,
                fixture.row.intent.product_id,
                fixture.row.intent.project_id,
                "devops",
                fixture.row.intent.operation_id,
                review,
                key,
                f'"{fixture.row.operation.revision}"',
                revalidate_human=AsyncMock(),
            )

    monkeypatch.setattr(evidence, "scope", hold)
    event.listen(test_engine.sync_engine, "before_cursor_execute", query)
    try:
        first = asyncio.create_task(complete("original-completion"))
        await asyncio.wait_for(locked.wait(), 10)
        second = asyncio.create_task(complete("original-completion" if same_key else "other-completion"))
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 20)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", query)
    assert results[0]["state"] == "succeeded", results
    if same_key:
        assert results[0] == results[1]
    else:
        assert isinstance(results[1], ObservationError) and results[1].code == "stale_revision", results
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeUseEvidence).where(KnowledgeUseEvidence.kind == "completed")) == 1
